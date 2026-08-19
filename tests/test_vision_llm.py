from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

import k12ta.transcribe.vision_llm as vision_llm
from k12ta.llm.base import (
    DataRetention,
    MisconfiguredError,
    RateLimitExhaustedError,
    RequestCapExceededError,
    TransientError,
    VisionResponse,
)
from k12ta.transcribe.base import FailureKind
from k12ta.transcribe.vision_llm import VisionLLMTranscriber


@dataclass
class FakeVisionModel:
    """Records the call it received and returns/raises a canned outcome. Test-only."""

    data_retention: DataRetention = DataRetention.NO_RETENTION
    response_text: str = '{"items": []}'
    error: Exception | None = None
    last_call: tuple[str, bytes, str] | None = None
    request_count: int = 0

    def generate(self, prompt: str, image_bytes: bytes, mime_type: str) -> VisionResponse:
        self.last_call = (prompt, image_bytes, mime_type)
        self.request_count += 1
        if self.error is not None:
            raise self.error
        return VisionResponse(text=self.response_text, cost_usd=Decimal("0"), latency_ms=42)

    def verify(self) -> None:
        self.request_count += 1
        if self.error is not None:
            raise self.error


def _transcriber(model: FakeVisionModel) -> VisionLLMTranscriber:
    return VisionLLMTranscriber(vision_model=model, provider="fake", model="fake-model")


def test_sends_raw_bytes_for_jpeg_and_the_base_prompt_with_no_placeholder_left(
    tmp_path: Path,
) -> None:
    """No `identity_schema` given -- discovery mode -- so nothing is known to
    look for yet; the {{SCHEMA_COMPONENTS}} placeholder must be filled in (never
    sent to the model literally), and the surrounding prompt text is unchanged."""
    image = tmp_path / "page.jpg"
    image.write_bytes(b"jpeg-bytes")
    model = FakeVisionModel()

    _transcriber(model).transcribe(str(image))

    assert model.last_call is not None
    prompt, image_bytes, mime_type = model.last_call
    assert "{{SCHEMA_COMPONENTS}}" not in prompt
    assert "Return JSON only" in prompt  # stable base-prompt text, unaffected
    assert image_bytes == b"jpeg-bytes"
    assert mime_type == "image/jpeg"


def test_sends_targeted_schema_components_in_the_prompt_when_given(tmp_path: Path) -> None:
    """Once a source's identity schema is known, the model is told exactly which
    named markers to look for -- not left to invent its own labels every call."""
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    model = FakeVisionModel()

    _transcriber(model).transcribe(
        str(image), identity_schema=[("day", "Day 5"), ("section", "Section 1")]
    )

    assert model.last_call is not None
    prompt, _, _ = model.last_call
    assert "{{SCHEMA_COMPONENTS}}" not in prompt
    assert '"day"' in prompt
    assert "Day 5" in prompt
    assert '"section"' in prompt
    assert "Section 1" in prompt


def test_converts_heic_via_sips_before_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "page.HEIC"
    image.write_bytes(b"heic-bytes")
    calls = []

    def fake_run(cmd: list[str], **kwargs: object) -> None:
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"converted-jpeg-bytes")

    monkeypatch.setattr(vision_llm.subprocess, "run", fake_run)
    model = FakeVisionModel()

    _transcriber(model).transcribe(str(image))

    assert calls and calls[0][0] == "sips"
    assert model.last_call is not None
    _, image_bytes, mime_type = model.last_call
    assert image_bytes == b"converted-jpeg-bytes"
    assert mime_type == "image/jpeg"


def test_parses_valid_response_into_items_with_no_failure(tmp_path: Path) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    payload = {
        "items": [
            {
                "problem_id": "1",
                "prompt_text": "2+2",
                "student_answer_raw": "4",
                "confidence": 0.9,
            }
        ]
    }
    model = FakeVisionModel(
        data_retention=DataRetention.PROVIDER_MAY_TRAIN, response_text=json.dumps(payload)
    )

    result = _transcriber(model).transcribe(str(image))

    assert result.failure is None
    assert result.failure_kind is None
    assert result.data_retention is DataRetention.PROVIDER_MAY_TRAIN
    assert result.cost_usd == 0.0
    assert result.latency_ms == 42
    assert len(result.items) == 1
    assert result.items[0].problem_id == "1"
    assert result.items[0].confidence == 0.9


def test_defaults_missing_or_invalid_confidence_to_zero(tmp_path: Path) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    payload = {
        "items": [
            {"problem_id": "1", "prompt_text": "p", "student_answer_raw": "a"},
            {
                "problem_id": "2",
                "prompt_text": "p",
                "student_answer_raw": "a",
                "confidence": "high",
            },
        ]
    }
    model = FakeVisionModel(response_text=json.dumps(payload))

    result = _transcriber(model).transcribe(str(image))

    assert result.failure is None
    assert [item.confidence for item in result.items] == [0.0, 0.0]


def test_blank_answer_confidence_is_clamped_to_zero_regardless_of_what_the_model_reports(
    tmp_path: Path,
) -> None:
    """A blank student_answer_raw can never carry a gradable confidence into the
    pipeline -- whatever the model reports there is a claim about "genuinely
    blank", not about a reading, and that claim type has never been calibrated
    (see docs/EVALS.md). Clamped here, at parse time, so no downstream code path
    can trust it; k12ta.grading.key_grader.grade_against_key's own blank check
    is the second, independent gate."""
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    payload = {
        "items": [
            {"problem_id": "1", "prompt_text": "p", "student_answer_raw": "", "confidence": 0.95},
            {
                "problem_id": "2",
                "prompt_text": "p",
                "student_answer_raw": "   ",
                "confidence": 0.99,
            },
        ]
    }
    model = FakeVisionModel(response_text=json.dumps(payload))

    result = _transcriber(model).transcribe(str(image))

    assert [item.confidence for item in result.items] == [0.0, 0.0]


def test_strips_markdown_code_fence_the_prompt_forbids(tmp_path: Path) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    fenced = '```json\n{"items": []}\n```'
    model = FakeVisionModel(response_text=fenced)

    result = _transcriber(model).transcribe(str(image))

    assert result.failure is None
    assert result.items == ()


def test_drops_non_dict_items_but_keeps_valid_ones(tmp_path: Path) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    payload = {
        "items": [
            "not a dict",
            {"problem_id": "1", "prompt_text": "p", "student_answer_raw": "a", "confidence": 1.0},
        ]
    }
    model = FakeVisionModel(response_text=json.dumps(payload))

    result = _transcriber(model).transcribe(str(image))

    assert result.failure is None
    assert len(result.items) == 1
    assert result.items[0].problem_id == "1"


def test_unparseable_response_returns_empty_items_with_failure_not_exception(
    tmp_path: Path,
) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    model = FakeVisionModel(response_text="the model just wrote prose instead of JSON")

    result = _transcriber(model).transcribe(str(image))

    assert result.items == ()
    assert result.failure is not None
    assert result.failure_kind is FailureKind.UNREADABLE
    assert result.data_retention is model.data_retention


def test_response_shaped_as_a_list_not_object_is_a_failure_not_a_silent_empty_success(
    tmp_path: Path,
) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    model = FakeVisionModel(response_text="[]")

    result = _transcriber(model).transcribe(str(image))

    assert result.items == ()
    assert result.failure is not None
    assert result.failure_kind is FailureKind.UNREADABLE


def test_vision_model_raising_never_propagates_and_sets_failure(tmp_path: Path) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    model = FakeVisionModel(error=RuntimeError("network exploded"))

    result = _transcriber(model).transcribe(str(image))

    assert result.items == ()
    assert result.failure is not None
    assert "network exploded" in result.failure
    assert result.failure_kind is FailureKind.UNREADABLE
    assert result.data_retention is model.data_retention


def test_unsupported_image_extension_is_a_failure_not_an_exception(tmp_path: Path) -> None:
    image = tmp_path / "page.gif"
    image.write_bytes(b"x")
    model = FakeVisionModel()

    result = _transcriber(model).transcribe(str(image))

    assert result.items == ()
    assert result.failure is not None
    assert result.failure_kind is FailureKind.UNREADABLE


def test_misconfigured_error_maps_to_misconfigured_failure_kind(tmp_path: Path) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    model = FakeVisionModel(error=MisconfiguredError("bad model name"))

    result = _transcriber(model).transcribe(str(image))

    assert result.items == ()
    assert result.failure_kind is FailureKind.MISCONFIGURED
    assert "bad model name" in (result.failure or "")


def test_rate_limit_exhausted_maps_to_rate_limited_failure_kind(tmp_path: Path) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    model = FakeVisionModel(error=RateLimitExhaustedError("rate-limited after 4 retries"))

    result = _transcriber(model).transcribe(str(image))

    assert result.items == ()
    assert result.failure_kind is FailureKind.RATE_LIMITED


def test_transient_error_maps_to_transient_failure_kind(tmp_path: Path) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    model = FakeVisionModel(error=TransientError("Gemini returned 503"))

    result = _transcriber(model).transcribe(str(image))

    assert result.items == ()
    assert result.failure_kind is FailureKind.TRANSIENT


def test_request_cap_exceeded_maps_to_request_cap_exceeded_failure_kind(tmp_path: Path) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    model = FakeVisionModel(error=RequestCapExceededError("reached the cap"))

    result = _transcriber(model).transcribe(str(image))

    assert result.items == ()
    assert result.failure_kind is FailureKind.REQUEST_CAP_EXCEEDED


def test_parses_page_identity_candidates_under_whatever_names_the_model_used(
    tmp_path: Path,
) -> None:
    """Scope B rework: candidate names are open-ended, not one of a fixed four --
    discovery mode has the model invent its own names, targeted mode uses the
    schema's names, and this layer doesn't care which; it just carries whatever
    string keys came back, feeding k12ta.grading.page_identity.resolve() the same
    shape either way: name -> every distinct value seen, plus one confidence for
    the whole extraction, separate from any item's answer confidence."""
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    payload = {
        "items": [],
        "page_identity": {
            "day": ["Day 1"],
            "worksheet_code": [],
            "section": ["Section 1"],
            "confidence": 0.9,
        },
    }
    model = FakeVisionModel(response_text=json.dumps(payload))

    result = _transcriber(model).transcribe(str(image))

    assert result.failure is None
    assert result.page_identity.candidates == {
        "day": ("Day 1",),
        "section": ("Section 1",),
    }
    assert result.page_identity.confidence == 0.9


def test_page_identity_conflicting_markers_on_a_spread_keep_every_distinct_value() -> None:
    """A two-page-spread photo can show two different "Day N" banners at once --
    the exact real-world shape the CONFLICTING outcome exists for (see
    tests/test_page_identity_resolution.py). The transcriber must preserve both
    values, not collapse or pick one -- the pick happens nowhere in this module."""
    from k12ta.transcribe.vision_llm import _parse_page_identity

    raw = {
        "day": ["Day 2", "Day 3"],
        "confidence": 0.85,
    }

    extraction = _parse_page_identity(raw)

    assert extraction.candidates["day"] == ("Day 2", "Day 3")


def test_missing_page_identity_defaults_to_no_candidates_and_zero_confidence(
    tmp_path: Path,
) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    model = FakeVisionModel(response_text='{"items": []}')

    result = _transcriber(model).transcribe(str(image))

    assert result.failure is None
    assert result.page_identity.candidates == {}
    assert result.page_identity.confidence == 0.0


def test_page_identity_ignores_the_confidence_key_and_non_string_values() -> None:
    """`confidence` is a sibling field, never a candidate name, however the model
    labels its markers -- and a non-string value in a candidate's list (the model
    not following the schema) is dropped rather than crashing the parse."""
    from k12ta.transcribe.vision_llm import _parse_page_identity

    raw = {
        "day": ["Day 1", 42, None],
        "section": ["Section 1"],
        "confidence": 0.5,
    }

    extraction = _parse_page_identity(raw)

    assert extraction.candidates == {"day": ("Day 1",), "section": ("Section 1",)}
    assert "confidence" not in extraction.candidates


def test_transcriber_exposes_request_count_from_the_vision_model(tmp_path: Path) -> None:
    image = tmp_path / "page.jpg"
    image.write_bytes(b"x")
    model = FakeVisionModel()
    transcriber = _transcriber(model)

    transcriber.transcribe(str(image))
    transcriber.transcribe(str(image))

    assert transcriber.request_count == 2 == model.request_count
