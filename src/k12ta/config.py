"""Runtime configuration. Read from environment, never hardcoded."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

COACH_NAME_PLACEHOLDER = "Coach"


@dataclass(frozen=True)
class Settings:
    """Process-wide settings.

    `coach_name` is a placeholder only. The real name is set by the student during
    setup and stored per student. Never treat this as the product name.
    """

    llm_provider: str
    llm_api_key: str
    llm_model: str
    data_dir: Path
    coach_name: str
    daily_token_budget_usd: Decimal
    log_level: str

    @staticmethod
    def from_env() -> Settings:
        return Settings(
            llm_provider=os.environ.get("K12TA_LLM_PROVIDER", "anthropic"),
            llm_api_key=os.environ.get("K12TA_LLM_API_KEY", ""),
            llm_model=os.environ.get("K12TA_LLM_MODEL", ""),
            data_dir=Path(os.environ.get("K12TA_DATA_DIR", "./data")),
            coach_name=os.environ.get("K12TA_COACH_NAME", COACH_NAME_PLACEHOLDER),
            daily_token_budget_usd=Decimal(
                os.environ.get("K12TA_DAILY_TOKEN_BUDGET_USD", "1.50")
            ),
            log_level=os.environ.get("K12TA_LOG_LEVEL", "INFO"),
        )
