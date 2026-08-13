from __future__ import annotations

import base64
import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from k12ta.llm.base import (
    DataRetention,
    MisconfiguredError,
    RateLimitExhaustedError,
    RequestCapExceededError,
    TransientError,
)
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


def _fake_clock(
    start: float = 0.0,
) -> tuple[Callable[[], float], Callable[[float], None], list[float]]:
    """A sleep that actually advances the paired clock, so throttle bookkeeping against
    a backoff sleep is deterministic instead of an accident of real wall-clock timing
    inside a fast test."""
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
) -> GeminiVisionModel:
    return GeminiVisionModel(
        api_key="test-key",
        model="gemini-3.7-flash",
        client=client,
        sleep=sleep or (lambda _: None),
        monotonic=monotonic or (lambda: 0.0),
        max_requests=max_requests,
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
    assert model.request_count == 1


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
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    with pytest.raises(httpx.HTTPStatusError):
        model.generate("p", b"x", "image/jpeg")

    assert len(calls) == 1
    assert waits == []


@pytest.mark.parametrize("status", [401, 403, 404])
def test_misconfigured_status_raises_without_retry(status: int) -> None:
    client, calls = _client_and_calls([httpx.Response(status, json={"error": "nope"})])
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    with pytest.raises(MisconfiguredError, match=str(status)):
        model.generate("p", b"x", "image/jpeg")

    assert len(calls) == 1
    assert waits == []


def test_misconfigured_error_names_the_model_to_check() -> None:
    client, _ = _client_and_calls([httpx.Response(404, json={"error": "not found"})])
    model = _model(client)

    with pytest.raises(MisconfiguredError, match="gemini-3.7-flash"):
        model.generate("p", b"x", "image/jpeg")


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_raises_transient_without_retry(status: int) -> None:
    client, calls = _client_and_calls([httpx.Response(status, json={"error": "down"})])
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    with pytest.raises(TransientError, match=str(status)):
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
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    response = model.generate("p", b"x", "image/jpeg")

    assert response.text == '{"items": []}'
    assert len(calls) == 3
    assert model.request_count == 3
    # Two 429s before success: two backoff sleeps, growing. The paired fake clock
    # advances by each backoff sleep, and every backoff (10s+) already clears the 5s
    # throttle floor, so integrating the throttle into the retry path adds no extra
    # waits here — it only matters when a retry follows a *short* prior sleep.
    assert len(waits) == 2
    assert waits[1] > waits[0]


def test_generate_raises_after_exhausting_retries() -> None:
    client, calls = _client_and_calls(
        [httpx.Response(429, json={"error": "rate limited"}) for _ in range(10)]
    )
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    with pytest.raises(RateLimitExhaustedError, match="retries"):
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
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    model.generate("p", b"x", "image/jpeg")
    model.generate("p", b"x", "image/jpeg")

    # The second call must wait for the minimum interval; the first must not.
    assert len(waits) == 1
    assert waits[0] > 0


def test_throttle_applies_between_retry_attempts_not_only_between_pages() -> None:
    # A retry whose backoff sleep barely advances the clock (faked here as 0.001s,
    # standing in for a shortened backoff configuration) must still be paced by the
    # same per-minute ceiling as any other request — the retry path is not a way to
    # bypass the throttle just because the constants happen not to expose it today.
    client, calls = _client_and_calls(
        [
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json=_envelope("{}")),
        ]
    )
    clock_state = {"now": 0.0}
    waits: list[float] = []

    def fake_monotonic() -> float:
        return clock_state["now"]

    def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        clock_state["now"] += 0.001  # far under the 5s throttle floor

    model = _model(client, fake_sleep, fake_monotonic)

    model.generate("p", b"x", "image/jpeg")

    assert len(calls) == 2
    # One backoff sleep (after the 429) and one throttle sleep (before the retry,
    # because the backoff only advanced the clock by 0.001s, far under the 5s floor).
    assert len(waits) == 2


def test_request_count_increases_with_each_attempt_including_retries() -> None:
    client, calls = _client_and_calls(
        [
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json=_envelope("{}")),
        ]
    )
    monotonic, sleep, _ = _fake_clock()
    model = _model(client, sleep, monotonic)

    model.generate("p", b"x", "image/jpeg")

    assert model.request_count == 3 == len(calls)


def test_request_cap_exceeded_raises_before_sending_the_next_request() -> None:
    client, calls = _client_and_calls([httpx.Response(200, json=_envelope("{}")) for _ in range(5)])
    monotonic, sleep, _ = _fake_clock()
    model = _model(client, sleep, monotonic, max_requests=2)

    model.generate("p", b"x", "image/jpeg")
    model.generate("p", b"x", "image/jpeg")
    with pytest.raises(RequestCapExceededError, match="2"):
        model.generate("p", b"x", "image/jpeg")

    assert len(calls) == 2
    assert model.request_count == 2


def test_verify_succeeds_on_200_and_makes_one_get_request() -> None:
    client, calls = _client_and_calls(
        [httpx.Response(200, json={"name": "models/gemini-3.7-flash"})]
    )
    model = _model(client)

    model.verify()

    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert calls[0].url.path == "/v1beta/models/gemini-3.7-flash"
    assert calls[0].headers["x-goog-api-key"] == "test-key"
    assert model.request_count == 1


def test_verify_raises_misconfigured_on_404() -> None:
    client, _ = _client_and_calls([httpx.Response(404, json={"error": "model not found"})])
    model = _model(client)

    with pytest.raises(MisconfiguredError, match="404"):
        model.verify()


def test_verify_raises_rate_limit_exhausted_after_retries() -> None:
    client, _ = _client_and_calls(
        [httpx.Response(429, json={"error": "rate limited"}) for _ in range(10)]
    )
    monotonic, sleep, _ = _fake_clock()
    model = _model(client, sleep, monotonic)

    with pytest.raises(RateLimitExhaustedError):
        model.verify()
