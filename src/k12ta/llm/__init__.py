"""Model-provider adapters. The only package allowed to call a model, per AGENTS.md rule 9."""

from __future__ import annotations

from k12ta.config import Settings
from k12ta.llm.base import TextModel, VisionModel
from k12ta.llm.gemini import GeminiVisionModel
from k12ta.llm.gemini_chat import GeminiTextModel


def build_vision_model(settings: Settings) -> VisionModel:
    if settings.llm_provider == "google":
        return GeminiVisionModel(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            max_requests=settings.llm_max_requests_per_run,
        )
    raise ValueError(f"unsupported LLM provider: {settings.llm_provider!r}")


def build_text_model(settings: Settings) -> TextModel:
    if settings.llm_provider == "google":
        return GeminiTextModel(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            max_requests=settings.llm_max_requests_per_run,
        )
    raise ValueError(f"unsupported LLM provider: {settings.llm_provider!r}")
