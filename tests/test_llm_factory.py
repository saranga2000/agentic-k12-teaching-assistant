from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from k12ta.config import Settings
from k12ta.llm import build_vision_model
from k12ta.llm.gemini import GeminiVisionModel


def _settings(provider: str) -> Settings:
    return Settings(
        llm_provider=provider,
        llm_api_key="key",
        llm_model="gemini-3.7-flash",
        llm_max_requests_per_run=40,
        data_dir=Path("./data"),
        coach_name="Coach",
        daily_token_budget_usd=Decimal("1.50"),
        daily_request_limit=20,
        log_level="INFO",
    )


def test_builds_gemini_model_for_google_provider() -> None:
    model = build_vision_model(_settings("google"))

    assert isinstance(model, GeminiVisionModel)
    assert model.model == "gemini-3.7-flash"


def test_raises_clearly_on_unsupported_provider() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        build_vision_model(_settings("openai"))
