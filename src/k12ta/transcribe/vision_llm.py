"""Vision LLM transcriber. Phase 1 implementation.

The prompt lives in prompts/transcribe_page.md and is loaded by id so that prompt
changes are reviewable and eval-able independently of this code.
"""

from __future__ import annotations

from k12ta.transcribe.base import TranscriptionResult


class VisionLLMTranscriber:
    """Single-reader transcription via a multimodal model.

    Implementation lands in M1 alongside its eval harness. Do not implement this
    before `evals/run_transcription_eval.py` can score it against labelled fixtures.
    """

    name = "vision_llm"

    def __init__(self, model: str, api_key: str) -> None:
        self._model = model
        self._api_key = api_key

    def transcribe(self, image_path: str) -> TranscriptionResult:
        raise NotImplementedError("M1: implement after the eval harness exists")
