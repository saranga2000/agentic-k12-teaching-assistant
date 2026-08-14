"""Gemini vision model adapter. Streaming HTTP logic specific to a vision call lives
here; the retry/backoff/throttle engine it shares with `k12ta.llm.gemini_chat` lives
in `k12ta.llm._gemini_http`.
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

from k12ta.llm._gemini_http import (
    API_BASE,
    DEFAULT_MAX_REQUESTS_PER_RUN,
    INITIAL_BACKOFF_SECONDS,
    MAX_RETRIES,
    MISCONFIGURED_STATUS_CODES,
    STREAM_INACTIVITY_TIMEOUT_SECONDS,
    GeminiHttpSession,
    default_client,
)
from k12ta.llm.base import (
    DataRetention,
    MisconfiguredError,
    RateLimitExhaustedError,
    RequestCapExceededError,
    TransientError,
    VisionResponse,
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
    client: httpx.Client = field(default_factory=default_client)
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    _session: GeminiHttpSession = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._session = GeminiHttpSession(
            api_key=self.api_key,
            model=self.model,
            max_requests=self.max_requests,
            client=self.client,
            sleep=self.sleep,
            monotonic=self.monotonic,
        )

    @property
    def request_count(self) -> int:
        return self._session.request_count

    @request_count.setter
    def request_count(self, value: int) -> None:
        # VisionModel's Protocol declares request_count as a plain settable
        # attribute; nothing in this codebase actually sets it from outside, but
        # a read-only property doesn't structurally satisfy that Protocol.
        self._session.request_count = value

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
        self._session.send_with_backoff("GET", url, json_body=None)

    def _stream_with_backoff(
        self,
        url: str,
        json_body: dict[str, Any],
        on_progress: Callable[[int], None] | None,
    ) -> str:
        """Like GeminiHttpSession.send_with_backoff (same throttle, same request cap
        check on every attempt, same 429/5xx retry-with-backoff), but for a streamed
        response, and deliberately NOT retrying a stall (httpx.TimeoutException while
        opening the connection or partway through reading the stream) the way a
        429/5xx does.

        That asymmetry is the point, not an oversight: a 429/503 comes back in well
        under a second, so retrying it is cheap. A stall means nothing has arrived
        for STREAM_INACTIVITY_TIMEOUT_SECONDS -- retrying immediately over the same
        path is unlikely to un-stick it fast, and an automatic retry there would
        multiply an already-large wait for little benefit. One honest, bounded
        failure; the caller's "Try again" is the retry, on a fresh connection, at a
        time the parent chooses.
        """
        session = self._session
        backoff = INITIAL_BACKOFF_SECONDS
        for attempt in range(MAX_RETRIES + 1):
            if session.request_count >= session.max_requests:
                raise RequestCapExceededError(
                    f"reached the configured request cap ({session.max_requests}) for "
                    "this run, including retries — aborting rather than continuing "
                    "to spend requests"
                )
            session.throttle()
            session.request_count += 1
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
                    if response.status_code in MISCONFIGURED_STATUS_CODES:
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
