"""Shared Gemini HTTP retry/backoff/throttle engine.

Used by both `k12ta.llm.gemini` (vision, streaming) and `k12ta.llm.gemini_chat`
(text conversation, non-streaming) so a second Gemini adapter doesn't duplicate the
retry engine wholesale. Not itself a public `k12ta.llm` entry point -- per AGENTS.md
rule 9, only those two modules are meant to construct a session directly.

Google's rate-limits page no longer publishes a static free-tier table (checked
ai.google.dev/gemini-api/docs/rate-limits directly); it defers to the account's own
AI Studio dashboard. This paces conservatively under the ~15 requests/minute the
developer's account is configured for, rather than assume a published number.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from k12ta.llm.base import (
    MisconfiguredError,
    RateLimitExhaustedError,
    RequestCapExceededError,
    TransientError,
)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
MIN_INTERVAL_SECONDS = 5.0  # ~12 requests/minute, under the ~15 rpm free-tier ceiling
MAX_RETRIES = 4
INITIAL_BACKOFF_SECONDS = 10.0
# A dead network is obvious immediately; no reason to wait long to find out.
CONNECT_TIMEOUT_SECONDS = 10.0
# An *inactivity* timeout, not a total-duration ceiling: httpx resets the read
# timeout on every chunk received from a streamed response, so this bounds "how
# long since anything arrived," never "how long the whole call takes." That
# distinction is why k12ta.llm.gemini's generate() uses streamGenerateContent (SSE)
# instead of a single blocking generateContent call -- a dense answer-key page can
# legitimately take minutes end to end, and a total-duration timeout has no way to
# tell that apart from a truly dead connection without being sized for the slowest
# page. Real measurement (2026-08-13, a genuinely dense Summer Bridge answer-key
# photo, streamed): first byte at 52.3s (the model "thinking" before any output),
# then chunks every <=2.5s apart until done at 92.9s. 100s gives roughly 2x margin
# over the one observed pre-first-byte gap while still failing well inside a few
# minutes on a truly stalled connection, instead of the 166.6s-plus a
# total-duration timeout would need merely to *equal* one successful call, with no
# margin left for a slower one. k12ta.llm.gemini_chat's non-streaming calls don't
# need this margin (a short chat reply, not a multi-minute OCR pass) but share the
# same client/timeout config for simplicity -- one slow call paying for headroom it
# doesn't need is cheaper than a second timeout profile to maintain.
STREAM_INACTIVITY_TIMEOUT_SECONDS = 100.0
# 1 preflight call + up to 4 non-exhausting retries/page (5 would trip the circuit
# breaker instead) across a 9-page corpus, plus slack. Raise via
# K12TA_LLM_MAX_REQUESTS_PER_RUN as the fixture corpus grows toward 40-60 pages.
DEFAULT_MAX_REQUESTS_PER_RUN = 40
# 401/403 (bad or missing key) and 404 (bad model name, verified against Google's own
# error-code reference for model_not_found) are never worth retrying or continuing
# past: every subsequent request in the run would fail identically.
MISCONFIGURED_STATUS_CODES = frozenset({401, 403, 404})


def default_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=STREAM_INACTIVITY_TIMEOUT_SECONDS,
            write=CONNECT_TIMEOUT_SECONDS,
            pool=CONNECT_TIMEOUT_SECONDS,
        )
    )


@dataclass
class GeminiHttpSession:
    """The mutable, per-run state (request count, last-request timestamp) and the
    retry/backoff/throttle logic both Gemini adapters share. Composed into each
    adapter rather than inherited, so each adapter's own dataclass keeps the exact
    constructor shape (`api_key`, `model`, `client`, `sleep`, `monotonic`,
    `max_requests`) it had before this session existed."""

    api_key: str
    model: str
    max_requests: int = DEFAULT_MAX_REQUESTS_PER_RUN
    client: httpx.Client = field(default_factory=default_client)
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    request_count: int = field(default=0, init=False)
    _last_request_at: float | None = field(default=None, init=False, repr=False)

    def throttle(self) -> None:
        if self._last_request_at is not None:
            elapsed = self.monotonic() - self._last_request_at
            if elapsed < MIN_INTERVAL_SECONDS:
                self.sleep(MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = self.monotonic()

    def send_with_backoff(
        self, method: str, url: str, *, json_body: dict[str, Any] | None
    ) -> httpx.Response:
        backoff = INITIAL_BACKOFF_SECONDS
        for attempt in range(MAX_RETRIES + 1):
            if self.request_count >= self.max_requests:
                raise RequestCapExceededError(
                    f"reached the configured request cap ({self.max_requests}) for "
                    "this run, including retries — aborting rather than continuing "
                    "to spend requests"
                )
            # Throttled and counted on every attempt, not just the first: a retry is a
            # real request and must be paced under the same per-minute ceiling as any
            # other, not exempted because it happens inside this loop.
            self.throttle()
            self.request_count += 1
            # The key goes in a header, never the URL: a query-param key ends up
            # verbatim in any exception message, log line, or report that mentions
            # the request URL, including this adapter's own failure reporting.
            response = self.client.request(
                method,
                url,
                json=json_body,
                headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            )
            # 429 and 5xx are both worth retrying: neither says anything about this
            # particular request being unrecoverable, unlike a 4xx that isn't 429 --
            # a 503 has no relation to the request's own content, so it gets the
            # exact same backoff treatment a rate limit does, capped by the same
            # MAX_RETRIES and, via the request_count check above, by the same
            # request cap on every attempt including these.
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < MAX_RETRIES:
                    self.sleep(backoff)
                    backoff *= 2
                    continue
                if response.status_code == 429:
                    raise RateLimitExhaustedError(
                        f"Gemini rate-limited after {MAX_RETRIES} retries"
                    )
                raise TransientError(
                    f"Gemini returned {response.status_code} after {MAX_RETRIES} retries"
                )
            if response.status_code in MISCONFIGURED_STATUS_CODES:
                raise MisconfiguredError(
                    f"Gemini returned {response.status_code} for model {self.model!r} "
                    "— check K12TA_LLM_MODEL and K12TA_LLM_API_KEY"
                )
            response.raise_for_status()
            return response
        raise AssertionError("unreachable: the loop above always returns or raises")
