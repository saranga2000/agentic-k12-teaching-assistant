"""Vision LLM transcriber: turns one page image into structured problems and answers.

Never raises, matching the Transcriber protocol's contract. A confidently wrong
transcription is worse than an honest failure, so any error here becomes an empty
result with `failure` set, not an exception propagating out of `transcribe`.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from k12ta.llm.base import VisionModel
from k12ta.prompts import load_prompt
from k12ta.transcribe.base import TranscribedItem, TranscriptionResult

_MIME_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


class VisionLLMTranscriber:
    """Single-reader transcription via a multimodal model."""

    name = "vision_llm"

    def __init__(self, vision_model: VisionModel, provider: str, model: str) -> None:
        self._vision_model = vision_model
        self._provider = provider
        self._model = model
        self._prompt = load_prompt("transcribe_page")

    def transcribe(self, image_path: str) -> TranscriptionResult:
        try:
            image_bytes, mime_type = _read_image(Path(image_path))
            response = self._vision_model.generate(self._prompt, image_bytes, mime_type)
            items = _parse_items(response.text)
            return TranscriptionResult(
                items=items,
                provider=self._provider,
                model=self._model,
                cost_usd=float(response.cost_usd),
                latency_ms=response.latency_ms,
                data_retention=self._vision_model.data_retention,
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


def _parse_items(text: str) -> tuple[TranscribedItem, ...]:
    payload = json.loads(_strip_code_fence(text))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("response JSON has no 'items' list")
    return tuple(_parse_item(item) for item in raw_items if isinstance(item, dict))


def _parse_item(raw: dict[str, object]) -> TranscribedItem:
    confidence = raw.get("confidence")
    valid_confidence = isinstance(confidence, int | float) and not isinstance(confidence, bool)
    return TranscribedItem(
        problem_id=str(raw.get("problem_id", "")),
        prompt_text=str(raw.get("prompt_text", "")),
        student_answer_raw=str(raw.get("student_answer_raw", "")),
        confidence=float(confidence) if valid_confidence else 0.0,  # type: ignore[arg-type]
    )


def _strip_code_fence(text: str) -> str:
    """The prompt says no markdown fence, but models do not always listen."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    return stripped.strip()
