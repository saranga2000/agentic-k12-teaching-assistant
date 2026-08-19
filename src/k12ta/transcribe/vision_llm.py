"""Vision LLM transcriber: turns one page image into structured problems and answers.

Never raises, matching the Transcriber protocol's contract. A confidently wrong
transcription is worse than an honest failure, so any error here becomes an empty
result with `failure` set, not an exception propagating out of `transcribe`.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

from k12ta.llm.base import (
    MisconfiguredError,
    RateLimitExhaustedError,
    RequestCapExceededError,
    TransientError,
    VisionModel,
)
from k12ta.prompts import load_prompt
from k12ta.transcribe._shared import build_identity_prompt, strip_code_fence
from k12ta.transcribe.base import (
    FailureKind,
    PageIdentityExtraction,
    TranscribedItem,
    TranscriptionResult,
)

_MIME_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}

# Adapter exceptions that mean "the run should stop" or "this page failed for a
# reason unrelated to its content" — everything else falls to UNREADABLE, the
# existing catch-all for a page the model genuinely could not read.
_FAILURE_KIND_BY_ERROR: dict[type[Exception], FailureKind] = {
    MisconfiguredError: FailureKind.MISCONFIGURED,
    RateLimitExhaustedError: FailureKind.RATE_LIMITED,
    RequestCapExceededError: FailureKind.REQUEST_CAP_EXCEEDED,
    TransientError: FailureKind.TRANSIENT,
}


def _classify(exc: Exception) -> FailureKind:
    for error_type, kind in _FAILURE_KIND_BY_ERROR.items():
        if isinstance(exc, error_type):
            return kind
    return FailureKind.UNREADABLE


class VisionLLMTranscriber:
    """Single-reader transcription via a multimodal model."""

    name = "vision_llm"

    def __init__(self, vision_model: VisionModel, provider: str, model: str) -> None:
        self._vision_model = vision_model
        self._provider = provider
        self._model = model
        self._prompt = load_prompt("transcribe_page")

    @property
    def request_count(self) -> int:
        return self._vision_model.request_count

    def transcribe(
        self, image_path: str, identity_schema: Sequence[tuple[str, str | None]] = ()
    ) -> TranscriptionResult:
        """`identity_schema` is this capture's source's current identity
        components, as `(component_name, example)` pairs in schema position
        order -- empty when the source has no schema yet (discovery mode: the
        model reports whatever it can see, under its own names). Built into the
        prompt fresh per call (see `_shared.build_identity_prompt`), never baked
        in at construction, since which components to look for is a fact about
        the source being read, not this transcriber instance."""
        try:
            image_bytes, mime_type = _read_image(Path(image_path))
            prompt = build_identity_prompt(self._prompt, identity_schema)
            response = self._vision_model.generate(prompt, image_bytes, mime_type)
            payload = _load_payload(response.text)
            items = _parse_items(payload)
            page_identity = _parse_page_identity(payload.get("page_identity"))
            return TranscriptionResult(
                items=items,
                provider=self._provider,
                model=self._model,
                cost_usd=float(response.cost_usd),
                latency_ms=response.latency_ms,
                data_retention=self._vision_model.data_retention,
                page_identity=page_identity,
            )
        except Exception as exc:
            return TranscriptionResult(
                items=(),
                provider=self._provider,
                model=self._model,
                cost_usd=0.0,
                latency_ms=0,
                data_retention=self._vision_model.data_retention,
                failure=f"{type(exc).__name__}: {exc}",
                failure_kind=_classify(exc),
            )


def _read_image(path: Path) -> tuple[bytes, str]:
    if path.suffix.lower() == ".heic":
        with tempfile.TemporaryDirectory() as tmp_dir:
            converted = Path(tmp_dir) / "page.jpg"
            subprocess.run(
                ["sips", "-s", "format", "jpeg", str(path), "--out", str(converted)],
                check=True,
                capture_output=True,
            )
            return converted.read_bytes(), "image/jpeg"
    mime_type = _MIME_TYPES.get(path.suffix.lower())
    if mime_type is None:
        raise ValueError(f"unsupported image type: {path.suffix}")
    return path.read_bytes(), mime_type


def _load_payload(text: str) -> dict[str, object]:
    payload = json.loads(strip_code_fence(text))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def _parse_items(payload: dict[str, object]) -> tuple[TranscribedItem, ...]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("response JSON has no 'items' list")
    return tuple(_parse_item(item) for item in raw_items if isinstance(item, dict))


def _parse_page_identity(raw: object) -> PageIdentityExtraction:
    """Missing or malformed `page_identity` is not an error -- a page with no
    legible marker at all is a real, expected shape, not a parse failure -- it
    just means no candidates, exactly the same "absent means nothing extracted"
    default `PageIdentityExtraction()` already carries.

    Candidate names are open-ended, not one of a fixed set -- discovery mode has
    the model invent its own names, targeted mode uses a source's schema names,
    and this layer carries whatever string key came back either way; only
    `confidence` (a sibling field, never a candidate) is excluded."""
    if not isinstance(raw, dict):
        return PageIdentityExtraction()
    candidates: dict[str, tuple[str, ...]] = {}
    for kind, raw_values in raw.items():
        if kind == "confidence" or not isinstance(kind, str) or not isinstance(raw_values, list):
            continue
        values = tuple(v for v in raw_values if isinstance(v, str) and v)
        if values:
            candidates[kind] = values
    confidence = raw.get("confidence")
    valid_confidence = isinstance(confidence, int | float) and not isinstance(confidence, bool)
    return PageIdentityExtraction(
        candidates=candidates,
        confidence=float(confidence) if valid_confidence else 0.0,  # type: ignore[arg-type]
    )


def _parse_item(raw: dict[str, object]) -> TranscribedItem:
    student_answer_raw = str(raw.get("student_answer_raw", ""))
    confidence = raw.get("confidence")
    valid_confidence = isinstance(confidence, int | float) and not isinstance(confidence, bool)
    reading_confidence = float(confidence) if valid_confidence else 0.0  # type: ignore[arg-type]
    return TranscribedItem(
        problem_id=str(raw.get("problem_id", "")),
        prompt_text=str(raw.get("prompt_text", "")),
        student_answer_raw=student_answer_raw,
        # A blank answer cannot carry a gradable confidence, full stop -- whatever
        # the model put in `confidence` here is its claim about `blank_confidence`
        # (see prompts/transcribe_page.md), a claim type docs/EVALS.md records as
        # never calibrated. Clamped at parse time, structurally, rather than left
        # for k12ta.grading.key_grader.grade_against_key's blank check to catch --
        # that check still exists too, as a second, independent gate.
        confidence=reading_confidence if student_answer_raw.strip() else 0.0,
    )
