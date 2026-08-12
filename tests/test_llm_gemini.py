from __future__ import annotations

import base64
import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from k12ta.llm.base import DataRetention
from k12ta.llm.gemini import REQUEST_TIMEOUT_SECONDS, GeminiError, GeminiVisionModel


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


def _sleeper() -> tuple[Callable[[float], None], list[float]]:
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)

    return sleep, waits


def _model(
    client: httpx.Client, sleep: Callable[[float], None] | None = None
) -> GeminiVisionModel:
    return GeminiVisionModel(
        api_key="test-key",
        model="gemini-3.7-flash",
        client=client,
        sleep=sleep or (lambda _: None),
    )


def test_default_client_uses_a_generous_timeout_not_httpxs_5s_default() -> None:
    model = GeminiVisionModel(api_key="k", model="m")

    assert model.client.timeout.read == REQUEST_TIMEOUT_SECONDS
    assert REQUEST_TIMEOUT_SECONDS > 5.0


def test_generate_returns_text_and_marks_provider_may_train() -> None:
    client, calls = _client_and_calls([httpx.Response(200, json=_envelope('{"items": []}'))])
    model = _model(client)

    response = model.generate("prompt text", b"fake-bytes", "image/jpeg")

    assert response.text == '{"items": []}'
    assert response.cost_usd == Decimal("0")
    assert response.latency_ms >= 0
    assert model.data_retention is DataRetention.PROVIDER_MAY_TRAIN
    assert len(calls) == 1
    assert calls[0].url.path == "/v1beta/models/gemini-3.7-flash:generateContent"
    assert calls[0].headers["x-goog-api-key"] == "test-key"


def test_api_key_never_appears_in_the_request_url() -> None:
    # A URL ends up verbatim in exception messages, logs, and eval reports. The key
    # must only ever travel in a header.
    client, calls = _client_and_calls([httpx.Response(200, json=_envelope("{}"))])
    model = _model(client)

    model.generate("p", b"x", "image/jpeg")

    assert "test-key" not in str(calls[0].url)
    assert "key" not in calls[0].url.params


def test_generate_sends_prompt_and_inline_image_data() -> None:
    client, calls = _client_and_calls([httpx.Response(200, json=_envelope("{}"))])
    model = _model(client)

    model.generate("describe this page", b"\x01\x02\x03", "image/png")

    body = json.loads(calls[0].content)
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"text": "describe this page"}
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert base64.b64decode(parts[1]["inline_data"]["data"]) == b"\x01\x02\x03"


def test_generate_raises_on_missing_candidates() -> None:
    client, _ = _client_and_calls([httpx.Response(200, json={"candidates": []})])
    model = _model(client)

    with pytest.raises(GeminiError, match="candidates"):
        model.generate("p", b"x", "image/jpeg")


def test_generate_raises_immediately_on_non_429_error_without_retry() -> None:
    client, calls = _client_and_calls([httpx.Response(400, json={"error": "bad request"})])
    sleep, waits = _sleeper()
    model = _model(client, sleep)

    with pytest.raises(httpx.HTTPStatusError):
        model.generate("p", b"x", "image/jpeg")

    assert len(calls) == 1
    assert waits == []


def test_generate_retries_on_429_then_succeeds() -> None:
    client, calls = _client_and_calls(
        [
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json=_envelope('{"items": []}')),
        ]
    )
    sleep, waits = _sleeper()
    model = _model(client, sleep)

    response = model.generate("p", b"x", "image/jpeg")

    assert response.text == '{"items": []}'
    assert len(calls) == 3
    # Two 429s before success: two backoff sleeps, growing.
    assert len(waits) == 2
    assert waits[1] > waits[0]


def test_generate_raises_after_exhausting_retries() -> None:
    client, calls = _client_and_calls(
        [httpx.Response(429, json={"error": "rate limited"}) for _ in range(10)]
    )
    sleep, waits = _sleeper()
    model = _model(client, sleep)

    with pytest.raises(GeminiError, match="retries"):
        model.generate("p", b"x", "image/jpeg")

    assert len(calls) > 1
    assert len(waits) == len(calls) - 1


def test_throttles_between_consecutive_calls() -> None:
    client, _ = _client_and_calls(
        [
            httpx.Response(200, json=_envelope("{}")),
            httpx.Response(200, json=_envelope("{}")),
        ]
    )
    sleep, waits = _sleeper()
    model = _model(client, sleep)

    model.generate("p", b"x", "image/jpeg")
    model.generate("p", b"x", "image/jpeg")

    # The second call must wait for the minimum interval; the first must not.
    assert len(waits) == 1
    assert waits[0] > 0
