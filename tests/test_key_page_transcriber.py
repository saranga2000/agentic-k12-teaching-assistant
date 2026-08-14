"""VisionLLMKeyTranscriber: same failure-classification contract as VisionLLMTranscriber,
different parsing (multi-page-per-photo entries, ungradeable reasons). No network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from k12ta.llm.base import (
    DataRetention,
    MisconfiguredError,
    RateLimitExhaustedError,
    RequestCapExceededError,
    TransientError,
    VisionResponse,
)
from k12ta.transcribe.base import FailureKind
from k12ta.transcribe.key_page import VisionLLMKeyTranscriber


@dataclass
class FakeVisionModel:
    """Records the call it received and returns/raises a canned outcome. Test-only."""

    data_retention: DataRetention = DataRetention.NO_RETENTION
    response_text: str = '{"entries": []}'
    error: Exception | None = None
    last_call: tuple[str, bytes, str] | None = None
    request_count: int = 0
    progress_updates: tuple[int, ...] = ()
    """Chars to report via on_progress, in order, before returning/raising -- stands
    in for a real streamed call's chunks arriving."""

    def generate(
        self,
        prompt: str,
        image_bytes: bytes,
        mime_type: str,
        on_progress: Callable[[int], None] | None = None,
    ) -> VisionResponse:
        self.last_call = (prompt, image_bytes, mime_type)
        self.request_count += 1
        if on_progress is not None:
            for chars in self.progress_updates:
                on_progress(chars)
        if self.error is not None:
            raise self.error
        return VisionResponse(text=self.response_text, cost_usd=Decimal("0"), latency_ms=42)

    def verify(self) -> None:
        self.request_count += 1
        if self.error is not None:
            raise self.error


def _transcriber(
    model: FakeVisionModel, monotonic: Callable[[], float] | None = None
) -> VisionLLMKeyTranscriber:
    kwargs = {} if monotonic is None else {"monotonic": monotonic}
    return VisionLLMKeyTranscriber(
        vision_model=model, provider="fake", model="fake-model", **kwargs
    )


def test_sends_bytes_as_jpeg_and_the_base_prompt_with_no_placeholder_left() -> None:
    model = FakeVisionModel()

    _transcriber(model).transcribe(b"already-normalized-jpeg-bytes")

    assert model.last_call is not None
    prompt, image_bytes, mime_type = model.last_call
    assert "{{SCHEMA_COMPONENTS}}" not in prompt
    assert "Return JSON only" in prompt  # stable base-prompt text, unaffected
    assert image_bytes == b"already-normalized-jpeg-bytes"
    assert mime_type == "image/jpeg"


def test_sends_targeted_schema_components_in_the_prompt_when_given() -> None:
    model = FakeVisionModel()

    _transcriber(model).transcribe(
        b"x", identity_schema=[("day", "Day 5"), ("section", "Section 1")]
    )

    assert model.last_call is not None
    prompt, _, _ = model.last_call
    assert '"day"' in prompt
    assert "Day 5" in prompt
    assert '"section"' in prompt
    assert "Section 1" in prompt


def test_parses_identity_values_alongside_page_number() -> None:
    """Scope B rework: a block's identity is a composite, not a single "Day N"
    string -- Summer Bridge needs section AND day together. `identity` is a dict,
    keyed by whatever component names were asked for (targeted mode) or whatever
    the model chose (discovery mode) -- see k12ta.store.page_identities and
    k12ta.grading.page_identity."""
    payload = {
        "entries": [
            {
                "page_number": 17,
                "identity": {"section": "Section 1", "day": "Day 5"},
                "problem_number": "1",
                "answer_text": "8 m",
                "ungradeable_reason": None,
                "confidence": 0.95,
            }
        ]
    }
    model = FakeVisionModel(response_text=json.dumps(payload))

    result = _transcriber(model).transcribe(b"x")

    assert result.entries[0].identity_values == {"section": "Section 1", "day": "Day 5"}


def test_missing_identity_defaults_to_empty_dict_not_an_error() -> None:
    payload = {
        "entries": [
            {
                "page_number": 17,
                "problem_number": "1",
                "answer_text": "8 m",
                "ungradeable_reason": None,
                "confidence": 0.95,
            }
        ]
    }
    model = FakeVisionModel(response_text=json.dumps(payload))

    result = _transcriber(model).transcribe(b"x")

    assert result.entries[0].identity_values == {}


def test_identity_drops_non_string_values_and_non_dict_identity() -> None:
    payload = {
        "entries": [
            {
                "page_number": 17,
                "identity": {"day": "Day 5", "junk": 42, "also_junk": None},
                "problem_number": "1",
                "answer_text": "8 m",
                "confidence": 0.95,
            },
            {
                "page_number": 18,
                "identity": "not a dict",
                "problem_number": "2",
                "answer_text": "9 m",
                "confidence": 0.95,
            },
        ]
    }
    model = FakeVisionModel(response_text=json.dumps(payload))

    result = _transcriber(model).transcribe(b"x")

    assert result.entries[0].identity_values == {"day": "Day 5"}
    assert result.entries[1].identity_values == {}


def test_parses_identifier_confidence_separately_from_answer_confidence() -> None:
    """The two confidences measure different things -- see KeyPageEntry's
    docstring -- so a model that is sure of the answer but unsure of the page
    heading (a smudged "Day 5" next to a crisp "8 m") must be able to say so."""
    payload = {
        "entries": [
            {
                "page_number": 17,
                "identity": {"day": "Day 5"},
                "problem_number": "1",
                "answer_text": "8 m",
                "ungradeable_reason": None,
                "confidence": 0.95,
                "identifier_confidence": 0.4,
            }
        ]
    }
    model = FakeVisionModel(response_text=json.dumps(payload))

    result = _transcriber(model).transcribe(b"x")

    assert result.entries[0].confidence == 0.95
    assert result.entries[0].identifier_confidence == 0.4


def test_missing_identifier_confidence_defaults_to_zero_not_an_error() -> None:
    payload = {
        "entries": [
            {
                "page_number": 17,
                "identity": {"day": "Day 5"},
                "problem_number": "1",
                "answer_text": "8 m",
                "ungradeable_reason": None,
                "confidence": 0.95,
            }
        ]
    }
    model = FakeVisionModel(response_text=json.dumps(payload))

    result = _transcriber(model).transcribe(b"x")

    assert result.entries[0].identifier_confidence == 0.0


def test_parses_entries_spanning_several_page_numbers_from_one_photo() -> None:
    payload = {
        "entries": [
            {
                "page_number": 17,
                "problem_number": "1",
                "answer_text": "8 m",
                "ungradeable_reason": None,
                "confidence": 0.95,
            },
            {
                "page_number": 18,
                "problem_number": "1",
                "answer_text": "15 cm",
                "ungradeable_reason": None,
                "confidence": 0.9,
            },
        ]
    }
    model = FakeVisionModel(response_text=json.dumps(payload))

    result = _transcriber(model).transcribe(b"x")

    assert result.failure is None
    assert [e.page_number for e in result.entries] == [17, 18]


def test_parses_answers_vary_and_graph_or_table_with_null_answer_text() -> None:
    payload = {
        "entries": [
            {
                "page_number": 20,
                "problem_number": "3",
                "answer_text": None,
                "ungradeable_reason": "answers_vary",
                "confidence": 0.9,
            },
            {
                "page_number": 20,
                "problem_number": "4",
                "answer_text": None,
                "ungradeable_reason": "graph_or_table",
                "confidence": 0.85,
            },
        ]
    }
    model = FakeVisionModel(response_text=json.dumps(payload))

    result = _transcriber(model).transcribe(b"x")

    assert result.failure is None
    assert result.entries[0].answer_text is None
    assert result.entries[0].ungradeable_reason == "answers_vary"
    assert result.entries[1].ungradeable_reason == "graph_or_table"


def test_defaults_missing_or_invalid_confidence_to_zero() -> None:
    payload = {
        "entries": [
            {"page_number": 1, "problem_number": "1", "answer_text": "a"},
            {
                "page_number": 1,
                "problem_number": "2",
                "answer_text": "a",
                "confidence": "high",
            },
        ]
    }
    model = FakeVisionModel(response_text=json.dumps(payload))

    result = _transcriber(model).transcribe(b"x")

    assert result.failure is None
    assert [e.confidence for e in result.entries] == [0.0, 0.0]


def test_strips_markdown_code_fence_the_prompt_forbids() -> None:
    model = FakeVisionModel(response_text='```json\n{"entries": []}\n```')

    result = _transcriber(model).transcribe(b"x")

    assert result.failure is None
    assert result.entries == ()


def test_drops_non_dict_entries_but_keeps_valid_ones() -> None:
    payload = {
        "entries": [
            "not a dict",
            {"page_number": 1, "problem_number": "1", "answer_text": "a", "confidence": 1.0},
        ]
    }
    model = FakeVisionModel(response_text=json.dumps(payload))

    result = _transcriber(model).transcribe(b"x")

    assert result.failure is None
    assert len(result.entries) == 1


def test_unparseable_response_returns_empty_entries_with_failure_not_exception() -> None:
    model = FakeVisionModel(response_text="the model just wrote prose instead of JSON")

    result = _transcriber(model).transcribe(b"x")

    assert result.entries == ()
    assert result.failure is not None
    assert result.failure_kind is FailureKind.UNREADABLE
    assert result.data_retention is model.data_retention


def test_vision_model_raising_never_propagates_and_sets_failure() -> None:
    model = FakeVisionModel(error=RuntimeError("network exploded"))

    result = _transcriber(model).transcribe(b"x")

    assert result.entries == ()
    assert result.failure is not None
    assert "network exploded" in result.failure
    assert result.failure_kind is FailureKind.UNREADABLE


def test_a_failed_call_reports_real_elapsed_time_not_a_hardcoded_zero() -> None:
    """A failure (a 503, a timeout) still cost real wall-clock time -- possibly
    tens of seconds of retries -- and that number is the one piece of evidence that
    would tell a parent's server log whether a stuck request was slow or stuck.
    Hardcoding latency_ms=0 on the failure path threw that evidence away."""
    model = FakeVisionModel(error=TransientError("Gemini returned 503"))
    clock = iter([100.0, 104.5])  # 4.5s elapsed before generate() raised

    result = _transcriber(model, monotonic=lambda: next(clock)).transcribe(b"x")

    assert result.failure is not None
    assert result.latency_ms == 4500


def test_misconfigured_error_maps_to_misconfigured_failure_kind() -> None:
    model = FakeVisionModel(error=MisconfiguredError("bad model name"))

    result = _transcriber(model).transcribe(b"x")

    assert result.failure_kind is FailureKind.MISCONFIGURED


def test_rate_limit_exhausted_maps_to_rate_limited_failure_kind() -> None:
    model = FakeVisionModel(error=RateLimitExhaustedError("rate-limited after 4 retries"))

    result = _transcriber(model).transcribe(b"x")

    assert result.failure_kind is FailureKind.RATE_LIMITED


def test_transient_error_maps_to_transient_failure_kind() -> None:
    model = FakeVisionModel(error=TransientError("Gemini returned 503"))

    result = _transcriber(model).transcribe(b"x")

    assert result.failure_kind is FailureKind.TRANSIENT


def test_request_cap_exceeded_maps_to_request_cap_exceeded_failure_kind() -> None:
    model = FakeVisionModel(error=RequestCapExceededError("reached the cap"))

    result = _transcriber(model).transcribe(b"x")

    assert result.failure_kind is FailureKind.REQUEST_CAP_EXCEEDED


def test_transcribe_passes_on_progress_through_to_the_vision_model() -> None:
    model = FakeVisionModel(progress_updates=(120, 340, 611))
    seen: list[int] = []

    _transcriber(model).transcribe(b"x", on_progress=seen.append)

    assert seen == [120, 340, 611]


def test_transcriber_exposes_request_count_from_the_vision_model() -> None:
    model = FakeVisionModel()
    transcriber = _transcriber(model)

    transcriber.transcribe(b"x")
    transcriber.transcribe(b"x")

    assert transcriber.request_count == 2 == model.request_count
