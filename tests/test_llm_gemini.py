from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
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
from k12ta.llm.gemini import (
    CONNECT_TIMEOUT_SECONDS,
    STREAM_INACTIVITY_TIMEOUT_SECONDS,
    GeminiError,
    GeminiVisionModel,
)


def _envelope(text: str) -> dict[str, object]:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _sse_response(*texts: str, status: int = 200) -> httpx.Response:
    """One httpx.Response whose body is a Server-Sent-Events stream, one `data:`
    line per text argument -- multiple arguments simulate the multi-chunk delivery
    a real streamGenerateContent call actually produces (confirmed empirically
    against the live API: a real dense key page arrived as 315 chunks). A single
    argument is the common case for tests that don't care about chunking itself."""
    body = "".join(f"data: {json.dumps(_envelope(t))}\n\n" for t in texts)
    return httpx.Response(status, content=body.encode("utf-8"))


def _client_and_calls(
    responses: list[httpx.Response],
) -> tuple[httpx.Client, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[len(calls) - 1]

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


class _RaisesReadTimeoutMidStream(httpx.SyncByteStream):
    """A response body stream that yields one real chunk, then raises ReadTimeout --
    standing in for a connection that goes silent partway through. MockTransport
    runs in-process with no real I/O, so there's no real clock for an actual
    timeout to fire against; this simulates the exact exception httpx raises for a
    real stall (confirmed empirically: `httpx.ReadTimeout: The read operation timed
    out`, forcing one against the live API with an absurdly short read timeout)."""

    def __iter__(self) -> Iterator[bytes]:
        yield f"data: {json.dumps(_envelope('partial'))}\n\n".encode()
        raise httpx.ReadTimeout(
            "The read operation timed out", request=httpx.Request("POST", "https://example.test")
        )

    def close(self) -> None:
        pass


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


def test_default_client_uses_a_short_connect_timeout_and_long_inactivity_timeout() -> None:
    model = GeminiVisionModel(api_key="k", model="m")

    # Asymmetric on purpose: connect should fail fast (a dead network is obvious
    # immediately), while read is an *inactivity* timeout, not a total-duration
    # ceiling -- httpx resets it on every chunk received, so this bounds "how long
    # since anything arrived," not "how long the whole call takes." See
    # STREAM_INACTIVITY_TIMEOUT_SECONDS's docstring in k12ta.llm.gemini for the
    # real measurement (a dense key page: first byte at 52.3s, then chunks every
    # <=2.5s until done at 92.9s) behind the 100s figure.
    assert model.client.timeout.connect == CONNECT_TIMEOUT_SECONDS
    assert model.client.timeout.read == STREAM_INACTIVITY_TIMEOUT_SECONDS
    assert model.client.timeout.read > model.client.timeout.connect


def test_generate_uses_the_streaming_endpoint() -> None:
    client, calls = _client_and_calls([_sse_response('{"items": []}')])
    model = _model(client)

    model.generate("prompt text", b"fake-bytes", "image/jpeg")

    assert calls[0].url.path == "/v1beta/models/gemini-3.7-flash:streamGenerateContent"
    assert calls[0].url.params["alt"] == "sse"


def test_generate_returns_text_and_marks_provider_may_train() -> None:
    client, calls = _client_and_calls([_sse_response('{"items": []}')])
    model = _model(client)

    response = model.generate("prompt text", b"fake-bytes", "image/jpeg")

    assert response.text == '{"items": []}'
    assert response.cost_usd == Decimal("0")
    assert response.latency_ms >= 0
    assert model.data_retention is DataRetention.PROVIDER_MAY_TRAIN
    assert len(calls) == 1
    assert calls[0].headers["x-goog-api-key"] == "test-key"
    assert model.request_count == 1


def test_generate_accumulates_text_across_multiple_stream_chunks() -> None:
    # Matches how a real response actually arrives -- one JSON object per SSE
    # line, concatenated in order, not one big blob in a single chunk.
    client, calls = _client_and_calls([_sse_response("Hello, ", "World", "!")])
    model = _model(client)

    response = model.generate("p", b"x", "image/jpeg")

    assert response.text == "Hello, World!"
    assert len(calls) == 1


def test_generate_reports_cumulative_chars_via_on_progress() -> None:
    # A parent watching a spinner has no way to tell "still working" from "stuck"
    # (see docs/ROADMAP.md's M2 note). on_progress is the hook the upload route uses
    # to show a live count instead. Cumulative, not per-chunk deltas: the caller
    # wants "how much has arrived so far," not arithmetic on a stream of diffs.
    client, _ = _client_and_calls([_sse_response("Hello, ", "World", "!")])
    model = _model(client)
    seen: list[int] = []

    model.generate("p", b"x", "image/jpeg", on_progress=seen.append)

    assert seen == [len("Hello, "), len("Hello, World"), len("Hello, World!")]


def test_api_key_never_appears_in_the_request_url() -> None:
    # A URL ends up verbatim in exception messages, logs, and eval reports. The key
    # must only ever travel in a header.
    client, calls = _client_and_calls([_sse_response("{}")])
    model = _model(client)

    model.generate("p", b"x", "image/jpeg")

    assert "test-key" not in str(calls[0].url)
    assert "key" not in calls[0].url.params


def test_generate_sends_prompt_and_inline_image_data() -> None:
    client, calls = _client_and_calls([_sse_response("{}")])
    model = _model(client)

    model.generate("describe this page", b"\x01\x02\x03", "image/png")

    body = json.loads(calls[0].content)
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"text": "describe this page"}
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert base64.b64decode(parts[1]["inline_data"]["data"]) == b"\x01\x02\x03"


def test_generate_raises_on_missing_candidates() -> None:
    body = f"data: {json.dumps({'candidates': []})}\n\n"
    client, _ = _client_and_calls([httpx.Response(200, content=body.encode())])
    model = _model(client)

    with pytest.raises(GeminiError, match="no text"):
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
def test_generate_retries_on_5xx_then_succeeds(status: int) -> None:
    """A dense key page's transcription call can hit a transient upstream 503 (or any
    5xx) with no relation to the page's own content -- worth exactly the same
    retry-with-backoff treatment as a 429, not an immediate dead end."""
    client, calls = _client_and_calls(
        [
            httpx.Response(status, json={"error": "down"}),
            httpx.Response(status, json={"error": "down"}),
            _sse_response('{"items": []}'),
        ]
    )
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    response = model.generate("p", b"x", "image/jpeg")

    assert response.text == '{"items": []}'
    assert len(calls) == 3
    assert len(waits) == 2
    assert waits[1] > waits[0]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_raises_transient_after_exhausting_retries(status: int) -> None:
    client, calls = _client_and_calls(
        [httpx.Response(status, json={"error": "down"}) for _ in range(10)]
    )
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    with pytest.raises(TransientError, match=str(status)):
        model.generate("p", b"x", "image/jpeg")

    assert len(calls) > 1
    assert len(waits) == len(calls) - 1


def test_5xx_retries_are_subject_to_the_request_cap_like_any_other_attempt() -> None:
    client, calls = _client_and_calls(
        [httpx.Response(503, json={"error": "down"}) for _ in range(5)]
    )
    monotonic, sleep, _ = _fake_clock()
    model = _model(client, sleep, monotonic, max_requests=2)

    with pytest.raises(RequestCapExceededError, match="2"):
        model.generate("p", b"x", "image/jpeg")

    assert len(calls) == 2
    assert model.request_count == 2


def test_generate_retries_on_429_then_succeeds() -> None:
    client, calls = _client_and_calls(
        [
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(429, json={"error": "rate limited"}),
            _sse_response('{"items": []}'),
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
    client, _ = _client_and_calls([_sse_response("{}"), _sse_response("{}")])
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
        [httpx.Response(429, json={"error": "rate limited"}), _sse_response("{}")]
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
            _sse_response("{}"),
        ]
    )
    monotonic, sleep, _ = _fake_clock()
    model = _model(client, sleep, monotonic)

    model.generate("p", b"x", "image/jpeg")

    assert model.request_count == 3 == len(calls)


def test_request_cap_exceeded_raises_before_sending_the_next_request() -> None:
    client, calls = _client_and_calls([_sse_response("{}") for _ in range(5)])
    monotonic, sleep, _ = _fake_clock()
    model = _model(client, sleep, monotonic, max_requests=2)

    model.generate("p", b"x", "image/jpeg")
    model.generate("p", b"x", "image/jpeg")
    with pytest.raises(RequestCapExceededError, match="2"):
        model.generate("p", b"x", "image/jpeg")

    assert len(calls) == 2
    assert model.request_count == 2


def test_stream_stall_raises_transient_with_no_retry() -> None:
    """The whole point of an inactivity timeout: unlike a 429/503, a stall this
    complete (nothing received for STREAM_INACTIVITY_TIMEOUT_SECONDS) is a rare,
    different failure -- retrying immediately over the same path is unlikely to
    un-stick it fast, and doing so would blow well past a bounded worst-case wait.
    One honest, fast-ish failure; the human "Try again" affordance is the retry."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_RaisesReadTimeoutMidStream())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    with pytest.raises(TransientError, match="stalled"):
        model.generate("p", b"x", "image/jpeg")

    assert model.request_count == 1  # no retry
    assert waits == []  # no backoff sleep either


def test_stream_timeout_before_any_data_raises_transient_with_no_retry() -> None:
    """Same no-retry treatment when the stall happens before even the first byte
    (the model's own "thinking" time, per the real measurement in
    STREAM_INACTIVITY_TIMEOUT_SECONDS's docstring) -- not just mid-stream."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monotonic, sleep, waits = _fake_clock()
    model = _model(client, sleep, monotonic)

    with pytest.raises(TransientError, match="stalled"):
        model.generate("p", b"x", "image/jpeg")

    assert model.request_count == 1
    assert waits == []


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
