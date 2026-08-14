from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from k12ta.llm.base import (
    ChatTurn,
    DataRetention,
    MisconfiguredError,
    RequestCapExceededError,
    TransientError,
)
from k12ta.llm.gemini_chat import GeminiTextModel


def _envelope(text: str) -> dict[str, object]:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _client_and_calls(
    responses: list[httpx.Response],
) -> tuple[httpx.Client, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[len(calls) - 1]

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def _fake_clock(
    start: float = 0.0,
) -> tuple[Callable[[], float], Callable[[float], None], list[float]]:
    now = [start]
    waits: list[float] = []

    def monotonic() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    return monotonic, sleep, waits


def _model(
    client: httpx.Client,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    max_requests: int = 100,
) -> GeminiTextModel:
    return GeminiTextModel(
        api_key="test-key",
        model="gemini-3.7-flash",
        client=client,
        sleep=sleep or (lambda _: None),
        monotonic=monotonic or (lambda: 0.0),
        max_requests=max_requests,
    )


def test_generate_conversation_uses_the_non_streaming_endpoint() -> None:
    client, calls = _client_and_calls([httpx.Response(200, json=_envelope("hi"))])
    model = _model(client)

    model.generate_conversation("system prompt", [ChatTurn(role="user", text="hello")])

    assert calls[0].url.path == "/v1beta/models/gemini-3.7-flash:generateContent"


def test_generate_conversation_sends_system_instruction_and_turns_in_order() -> None:
    client, calls = _client_and_calls([httpx.Response(200, json=_envelope("hi"))])
    model = _model(client)

    model.generate_conversation(
        "you are a coach",
        [
            ChatTurn(role="user", text="just tell me the answer"),
            ChatTurn(role="model", text="let's look at your method instead"),
            ChatTurn(role="user", text="is it 14?"),
        ],
    )

    body = json.loads(calls[0].content)
    assert body["systemInstruction"]["parts"] == [{"text": "you are a coach"}]
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "just tell me the answer"}]},
        {"role": "model", "parts": [{"text": "let's look at your method instead"}]},
        {"role": "user", "parts": [{"text": "is it 14?"}]},
    ]


def test_generate_conversation_returns_text_and_marks_provider_may_train() -> None:
    client, calls = _client_and_calls([httpx.Response(200, json=_envelope("Not quite."))])
    model = _model(client)

    response = model.generate_conversation("sys", [ChatTurn(role="user", text="is it 14?")])

    assert response.text == "Not quite."
    assert response.cost_usd == Decimal("0")
    assert response.latency_ms >= 0
    assert model.data_retention is DataRetention.PROVIDER_MAY_TRAIN
    assert len(calls) == 1
    assert calls[0].headers["x-goog-api-key"] == "test-key"
    assert model.request_count == 1


def test_api_key_never_appears_in_the_request_url() -> None:
    client, calls = _client_and_calls([httpx.Response(200, json=_envelope("hi"))])
    model = _model(client)

    model.generate_conversation("sys", [ChatTurn(role="user", text="hi")])

    assert "test-key" not in str(calls[0].url)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_misconfigured_status_raises_without_retry(status: int) -> None:
    client, calls = _client_and_calls([httpx.Response(status, json={"error": "nope"})])
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    with pytest.raises(MisconfiguredError, match=str(status)):
        model.generate_conversation("sys", [ChatTurn(role="user", text="hi")])

    assert len(calls) == 1
    assert waits == []


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_generate_conversation_retries_on_5xx_then_succeeds(status: int) -> None:
    client, calls = _client_and_calls(
        [
            httpx.Response(status, json={"error": "down"}),
            httpx.Response(status, json={"error": "down"}),
            httpx.Response(200, json=_envelope("hi")),
        ]
    )
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    response = model.generate_conversation("sys", [ChatTurn(role="user", text="hi")])

    assert response.text == "hi"
    assert len(calls) == 3
    assert len(waits) == 2
    assert waits[1] > waits[0]


def test_generate_conversation_retries_on_429_then_succeeds() -> None:
    client, calls = _client_and_calls(
        [
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json=_envelope("hi")),
        ]
    )
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    response = model.generate_conversation("sys", [ChatTurn(role="user", text="hi")])

    assert response.text == "hi"
    assert model.request_count == 2


def test_5xx_raises_transient_after_exhausting_retries() -> None:
    client, calls = _client_and_calls(
        [httpx.Response(503, json={"error": "down"}) for _ in range(10)]
    )
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    with pytest.raises(TransientError, match="503"):
        model.generate_conversation("sys", [ChatTurn(role="user", text="hi")])

    assert len(calls) > 1


def test_request_cap_is_respected() -> None:
    client, calls = _client_and_calls(
        [httpx.Response(503, json={"error": "down"}) for _ in range(5)]
    )
    monotonic, sleep, _ = _fake_clock()
    model = _model(client, sleep, monotonic, max_requests=2)

    with pytest.raises(RequestCapExceededError, match="2"):
        model.generate_conversation("sys", [ChatTurn(role="user", text="hi")])

    assert len(calls) == 2
    assert model.request_count == 2


def test_verify_makes_one_get_call() -> None:
    client, calls = _client_and_calls(
        [httpx.Response(200, json={"name": "models/gemini-3.7-flash"})]
    )
    model = _model(client)

    model.verify()

    assert calls[0].method == "GET"
    assert calls[0].url.path == "/v1beta/models/gemini-3.7-flash"
