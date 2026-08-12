"""Gemini vision model adapter. The only place Gemini-specific HTTP logic lives.

Google's rate-limits page no longer publishes a static free-tier table (checked
ai.google.dev/gemini-api/docs/rate-limits directly); it defers to the account's own
AI Studio dashboard. This paces conservatively under the ~15 requests/minute the
developer's account is configured for, rather than assume a published number.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from k12ta.llm.base import DataRetention, VisionResponse

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
MIN_INTERVAL_SECONDS = 5.0  # ~12 requests/minute, under the ~15 rpm free-tier ceiling
MAX_RETRIES = 4
INITIAL_BACKOFF_SECONDS = 10.0
# httpx.Client()'s bare default is 5s total, far too short for a base64-encoded photo
# plus vision-model generation time.
REQUEST_TIMEOUT_SECONDS = 60.0


def _default_client() -> httpx.Client:
    return httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)


class GeminiError(RuntimeError):
    """Gemini returned something the adapter could not turn into text."""


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
    client: httpx.Client = field(default_factory=_default_client)
    sleep: Callable[[float], None] = time.sleep
    _last_request_at: float | None = field(default=None, init=False, repr=False)

    def generate(self, prompt: str, image_bytes: bytes, mime_type: str) -> VisionResponse:
        self._throttle()
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
        url = f"{API_BASE}/models/{self.model}:generateContent"
        started = time.monotonic()
        response = self._post_with_backoff(url, body)
        latency_ms = int((time.monotonic() - started) * 1000)

        text = _extract_text(response.json())
        return VisionResponse(text=text, cost_usd=Decimal("0"), latency_ms=latency_ms)

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < MIN_INTERVAL_SECONDS:
                self.sleep(MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _post_with_backoff(self, url: str, body: dict[str, Any]) -> httpx.Response:
        backoff = INITIAL_BACKOFF_SECONDS
        for attempt in range(MAX_RETRIES + 1):
            # The key goes in a header, never the URL: a query-param key ends up
            # verbatim in any exception message, log line, or report that mentions
            # the request URL, including this adapter's own failure reporting.
            response = self.client.post(
                url,
                json=body,
                headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            )
            if response.status_code != 429:
                response.raise_for_status()
                return response
            if attempt < MAX_RETRIES:
                self.sleep(backoff)
                backoff *= 2
        raise GeminiError(f"Gemini rate-limited after {MAX_RETRIES} retries")


def _extract_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise GeminiError(f"no candidates in Gemini response: {payload!r}")
    first = candidates[0]
    content = first.get("content") if isinstance(first, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list) or not parts:
        raise GeminiError(f"no content parts in Gemini response: {payload!r}")
    text = parts[0].get("text") if isinstance(parts[0], dict) else None
    if not isinstance(text, str):
        raise GeminiError(f"no text in Gemini response part: {payload!r}")
    return text
