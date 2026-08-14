"""Gemini vision model adapter. The only place Gemini-specific HTTP logic lives.

Google's rate-limits page no longer publishes a static free-tier table (checked
ai.google.dev/gemini-api/docs/rate-limits directly); it defers to the account's own
AI Studio dashboard. This paces conservatively under the ~15 requests/minute the
developer's account is configured for, rather than assume a published number.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from k12ta.llm.base import (
    DataRetention,
    MisconfiguredError,
    RateLimitExhaustedError,
    RequestCapExceededError,
    TransientError,
    VisionResponse,
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
# distinction is why generate() uses streamGenerateContent (SSE) instead of a
# single blocking generateContent call -- a dense answer-key page can legitimately
# take minutes end to end, and a total-duration timeout has no way to tell that
# apart from a truly dead connection without being sized for the slowest page.
# Real measurement (2026-08-13, a genuinely dense Summer Bridge answer-key photo,
# streamed): first byte at 52.3s (the model "thinking" before any output), then
# chunks every <=2.5s apart until done at 92.9s. 100s gives roughly 2x margin over
# the one observed pre-first-byte gap while still failing well inside a few
# minutes on a truly stalled connection, instead of the 166.6s-plus a
# total-duration timeout would need merely to *equal* one successful call, with no
# margin left for a slower one.
STREAM_INACTIVITY_TIMEOUT_SECONDS = 100.0
# 1 preflight call + up to 4 non-exhausting retries/page (5 would trip the circuit
# breaker instead) across a 9-page corpus, plus slack. Raise via
# K12TA_LLM_MAX_REQUESTS_PER_RUN as the fixture corpus grows toward 40-60 pages.
DEFAULT_MAX_REQUESTS_PER_RUN = 40
# 401/403 (bad or missing key) and 404 (bad model name, verified against Google's own
# error-code reference for model_not_found) are never worth retrying or continuing
# past: every subsequent request in the run would fail identically.
_MISCONFIGURED_STATUS_CODES = frozenset({401, 403, 404})


def _default_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=STREAM_INACTIVITY_TIMEOUT_SECONDS,
            write=CONNECT_TIMEOUT_SECONDS,
            pool=CONNECT_TIMEOUT_SECONDS,
        )
    )


class GeminiError(RuntimeError):
    """Gemini returned a response this adapter could not turn into text. Distinct from
    the classified errors in k12ta.llm.base: this is a malformed-but-delivered
    response, not a request-level failure, so it stays on the existing low-confidence
    path rather than aborting a run."""


@dataclass
class GeminiVisionModel:
    """Calls the Gemini API's generateContent endpoint over raw HTTP.

    Free tier: Google's terms permit using submitted content, including human review,
    to improve its products (verified at ai.google.dev/gemini-api/terms_preview). That
    is a property of the tier, not of any single response, so it is a fixed attribute
    of this adapter rather than something parsed off an API reply.
    """

    api_key: str
    model: str
    data_retention: DataRetention = field(default=DataRetention.PROVIDER_MAY_TRAIN)
    max_requests: int = DEFAULT_MAX_REQUESTS_PER_RUN
    client: httpx.Client = field(default_factory=_default_client)
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    request_count: int = field(default=0, init=False)
    _last_request_at: float | None = field(default=None, init=False, repr=False)

    def generate(
        self,
        prompt: str,
        image_bytes: bytes,
        mime_type: str,
        on_progress: Callable[[int], None] | None = None,
    ) -> VisionResponse:
        """`on_progress`, if given, is called with the cumulative character count
        received so far, once per stream chunk that carries text -- the signal a
        caller uses to show something better than a static spinner for a call that
        can legitimately run a couple of minutes (see STREAM_INACTIVITY_TIMEOUT_
        SECONDS's docstring). Cumulative, not a per-chunk delta: callers want "how
        much has arrived," not arithmetic on a stream of diffs."""
        body = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        },
                    ]
                }
            ]
        }
        url = f"{API_BASE}/models/{self.model}:streamGenerateContent?alt=sse"
        started = self.monotonic()
        text = self._stream_with_backoff(url, body, on_progress)
        latency_ms = int((self.monotonic() - started) * 1000)
        return VisionResponse(text=text, cost_usd=Decimal("0"), latency_ms=latency_ms)

    def verify(self) -> None:
        """One cheap call confirming `model` exists and `api_key` works: model
        metadata only, no generation, no token cost. Raises MisconfiguredError or
        RateLimitExhaustedError on failure; raises nothing on success."""
        url = f"{API_BASE}/models/{self.model}"
        self._send_with_backoff("GET", url, json_body=None)

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            elapsed = self.monotonic() - self._last_request_at
            if elapsed < MIN_INTERVAL_SECONDS:
                self.sleep(MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = self.monotonic()

    def _send_with_backoff(
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
            self._throttle()
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
            # a 503 has no relation to the page's own content, so it gets the exact
            # same backoff treatment a rate limit does, capped by the same
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
            if response.status_code in _MISCONFIGURED_STATUS_CODES:
                raise MisconfiguredError(
                    f"Gemini returned {response.status_code} for model {self.model!r} "
                    "— check K12TA_LLM_MODEL and K12TA_LLM_API_KEY"
                )
            response.raise_for_status()
            return response
        raise AssertionError("unreachable: the loop above always returns or raises")

    def _stream_with_backoff(
        self,
        url: str,
        json_body: dict[str, Any],
        on_progress: Callable[[int], None] | None,
    ) -> str:
        """Like _send_with_backoff (same throttle, same request cap check on every
        attempt, same 429/5xx retry-with-backoff), but for a streamed response, and
        deliberately NOT retrying a stall (httpx.TimeoutException while opening the
        connection or partway through reading the stream) the way a 429/5xx does.

        That asymmetry is the point, not an oversight: a 429/503 comes back in well
        under a second, so retrying it is cheap. A stall means nothing has arrived
        for STREAM_INACTIVITY_TIMEOUT_SECONDS -- retrying immediately over the same
        path is unlikely to un-stick it fast, and an automatic retry there would
        multiply an already-large wait for little benefit. One honest, bounded
        failure; the caller's "Try again" is the retry, on a fresh connection, at a
        time the parent chooses.
        """
        backoff = INITIAL_BACKOFF_SECONDS
        for attempt in range(MAX_RETRIES + 1):
            if self.request_count >= self.max_requests:
                raise RequestCapExceededError(
                    f"reached the configured request cap ({self.max_requests}) for "
                    "this run, including retries — aborting rather than continuing "
                    "to spend requests"
                )
            self._throttle()
            self.request_count += 1
            try:
                with self.client.stream(
                    "POST",
                    url,
                    json=json_body,
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": self.api_key,
                    },
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        response.read()
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
                    if response.status_code in _MISCONFIGURED_STATUS_CODES:
                        raise MisconfiguredError(
                            f"Gemini returned {response.status_code} for model "
                            f"{self.model!r} — check K12TA_LLM_MODEL and K12TA_LLM_API_KEY"
                        )
                    response.raise_for_status()
                    return _accumulate_stream_text(response, on_progress)
            except httpx.TimeoutException as exc:
                raise TransientError(
                    f"Gemini stream stalled: no data received for "
                    f"{STREAM_INACTIVITY_TIMEOUT_SECONDS:.0f}s"
                ) from exc
        raise AssertionError("unreachable: the loop above always returns or raises")


def _accumulate_stream_text(
    response: httpx.Response, on_progress: Callable[[int], None] | None
) -> str:
    """Concatenate every `parts[].text` across a streamGenerateContent SSE
    response's chunks, in arrival order. A "thinking" model (see
    STREAM_INACTIVITY_TIMEOUT_SECONDS's docstring) sends chunks with a
    thoughtSignature and no text at all -- silently skipped, not an error, since
    they carry no content this adapter cares about, and `on_progress` is not called
    for them either: nothing changed a caller watching the running character count
    would want to hear about."""
    chunks: list[str] = []
    total_chars = 0
    for line in response.iter_lines():
        if not line.startswith("data:"):
            continue
        payload = json.loads(line[len("data:") :].strip())
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            continue
        first = candidates[0]
        content = first.get("content") if isinstance(first, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text = part["text"]
                if not text:
                    continue
                chunks.append(text)
                total_chars += len(text)
                if on_progress is not None:
                    on_progress(total_chars)
    if not chunks:
        raise GeminiError("no text in Gemini stream response")
    return "".join(chunks)
