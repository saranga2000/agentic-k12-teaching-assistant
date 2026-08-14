"""Gemini text-conversation adapter: a system prompt plus a turn history in, the next
turn's text out -- no image. Built on the same retry/backoff/throttle engine as the
vision adapter (`k12ta.llm._gemini_http`), but a single non-streaming `generateContent`
call: a chat reply is short, unlike a multi-minute vision OCR pass, so none of
`k12ta.llm.gemini`'s SSE/on_progress machinery is needed here.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import httpx

from k12ta.llm._gemini_http import (
    API_BASE,
    DEFAULT_MAX_REQUESTS_PER_RUN,
    GeminiHttpSession,
    default_client,
)
from k12ta.llm.base import ChatResponse, ChatTurn, DataRetention


class GeminiChatError(RuntimeError):
    """Gemini returned a response this adapter could not turn into text -- a
    malformed-but-delivered response, not a request-level failure (those are the
    classified errors in k12ta.llm.base)."""


@dataclass
class GeminiTextModel:
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
        self._session.request_count = value

    def generate_conversation(self, system_prompt: str, turns: Sequence[ChatTurn]) -> ChatResponse:
        """`turns` must end with a "user" turn -- there is nothing to respond to
        otherwise, and an empty conversation is a caller bug, not a degradable
        runtime condition."""
        assert turns, "generate_conversation needs at least one turn"
        assert turns[-1].role == "user", "the last turn must be the student's"
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": t.role, "parts": [{"text": t.text}]} for t in turns],
        }
        url = f"{API_BASE}/models/{self.model}:generateContent"
        started = self.monotonic()
        response = self._session.send_with_backoff("POST", url, json_body=body)
        text = _extract_text(response)
        latency_ms = int((self.monotonic() - started) * 1000)
        return ChatResponse(text=text, cost_usd=Decimal("0"), latency_ms=latency_ms)

    def verify(self) -> None:
        """One cheap call confirming `model` exists and `api_key` works: model
        metadata only, no generation, no token cost."""
        url = f"{API_BASE}/models/{self.model}"
        self._session.send_with_backoff("GET", url, json_body=None)


def _extract_text(response: httpx.Response) -> str:
    payload = response.json()
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise GeminiChatError("no candidates in Gemini response")
    first = candidates[0]
    content = first.get("content") if isinstance(first, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise GeminiChatError("no content parts in Gemini response")
    texts = [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]
    if not texts:
        raise GeminiChatError("no text in Gemini response")
    return "".join(texts)
