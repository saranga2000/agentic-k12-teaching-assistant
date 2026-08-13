"""Runtime configuration. Read from environment, never hardcoded."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

COACH_NAME_PLACEHOLDER = "Coach"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path = _REPO_ROOT / ".env") -> None:
    """Minimal .env loader: KEY=value lines, comments and blank lines ignored, an
    already-set real environment variable always wins over the file. No new
    dependency (python-dotenv) for a handful of lines.

    The single shared implementation for every entry point that needs it --
    `k12ta.web.app` and `evals/run_transcription_eval.py` -- so `.env` support
    can't quietly work in one and not the other.
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    """Process-wide settings.

    `coach_name` is a placeholder only. The real name is set by the student during
    setup and stored per student. Never treat this as the product name.
    """

    llm_provider: str
    llm_api_key: str
    llm_model: str
    llm_max_requests_per_run: int
    data_dir: Path
    coach_name: str
    daily_token_budget_usd: Decimal
    daily_request_limit: int
    """Hard ceiling on transcribe attempts per calendar day, persisted in the database
    (see k12ta.store.quota) so a server restart cannot reset it. Default 20: two
    children, a handful of pages per sitting, generous headroom, still a real ceiling.
    Raise it via K12TA_DAILY_REQUEST_LIMIT once real usage patterns are known."""
    log_level: str

    @staticmethod
    def from_env() -> Settings:
        return Settings(
            llm_provider=os.environ.get("K12TA_LLM_PROVIDER", "anthropic"),
            llm_api_key=os.environ.get("K12TA_LLM_API_KEY", ""),
            llm_model=os.environ.get("K12TA_LLM_MODEL", ""),
            llm_max_requests_per_run=int(os.environ.get("K12TA_LLM_MAX_REQUESTS_PER_RUN", "40")),
            data_dir=Path(os.environ.get("K12TA_DATA_DIR", "./data")),
            coach_name=os.environ.get("K12TA_COACH_NAME", COACH_NAME_PLACEHOLDER),
            daily_token_budget_usd=Decimal(os.environ.get("K12TA_DAILY_TOKEN_BUDGET_USD", "1.50")),
            daily_request_limit=int(os.environ.get("K12TA_DAILY_REQUEST_LIMIT", "20")),
            log_level=os.environ.get("K12TA_LOG_LEVEL", "INFO"),
        )
