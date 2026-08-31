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
    (see k12ta.store.quota) so a server restart cannot reset it. This is a local safety
    valve against a runaway loop, not an attempt to match Google's own free-tier ceiling
    -- Google no longer publishes a static rate-limit table (checked ai.google.dev/
    gemini-api/docs/rate-limits directly, 2026-08-20); it defers to the account's own AI
    Studio dashboard, and this setting has no relationship to that number at all. It also
    does not gate evals/integrity/run.py's live eval calls, which never check it. Default
    60: three times a planned 20-photo batch of headroom for one real day's use, while
    still stopping a genuine runaway well short of hundreds of calls unattended. Raise it
    via K12TA_DAILY_REQUEST_LIMIT if real usage patterns need more."""
    log_level: str
    parent_pin: str | None = None
    """Gates exactly one action (docs/ARCHITECTURE.md): overriding a source's
    feedback mode (k12ta.domain.policy.resolve_mode's `parent_override`) from
    k12ta.keys. Not a login -- checked inline against the submitted form
    field on that one POST, no session or cookie created, nothing else in
    either app reads it. `None` (unset, the default) refuses the action
    outright rather than silently accepting a blank PIN; AGENTS.md rule 8
    still holds ("do not build authentication") because this gates one
    write, not access to anything."""
    evaluator_enabled: bool = False
    """M6, docs/ROADMAP.md's "agentic evaluator" -- the tier-2/tier-3 ladder in
    k12ta.grading.evaluator. Default False: with this unset, k12ta.pipeline.
    process never calls it at all, so shipping this code changes nothing about
    what a real deployment does today. A parent/deployer turns it on
    deliberately, per M6's own "ships behind a flag" requirement."""
    evaluator_mark_wrong_enabled: bool = False
    """The second, independent half of M6's flag requirement, and the more
    important one: even with evaluator_enabled True, an evaluator-produced
    INCORRECT is never shown to a child as INCORRECT while this is False --
    it downgrades to NEEDS_HUMAN so a parent sees it first, regardless of the
    evaluator's own confidence. Per docs/ROADMAP.md's V1 definition, this
    stays False until docs/EVALS.md family 3's precision number exists and
    clears a stated threshold on real fixtures -- not a default to casually
    flip because the first few keyless grades looked right."""

    @staticmethod
    def from_env() -> Settings:
        return Settings(
            llm_provider=os.environ.get("K12TA_LLM_PROVIDER", "anthropic"),
            llm_api_key=os.environ.get("K12TA_LLM_API_KEY", ""),
            llm_model=os.environ.get("K12TA_LLM_MODEL", ""),
            llm_max_requests_per_run=int(os.environ.get("K12TA_LLM_MAX_REQUESTS_PER_RUN", "40")),
            data_dir=Path(os.environ.get("K12TA_DATA_DIR", "./data")),
            coach_name=os.environ.get("K12TA_COACH_NAME", COACH_NAME_PLACEHOLDER),
            parent_pin=os.environ.get("K12TA_PARENT_PIN") or None,
            daily_token_budget_usd=Decimal(os.environ.get("K12TA_DAILY_TOKEN_BUDGET_USD", "1.50")),
            daily_request_limit=int(os.environ.get("K12TA_DAILY_REQUEST_LIMIT", "60")),
            log_level=os.environ.get("K12TA_LOG_LEVEL", "INFO"),
            evaluator_enabled=os.environ.get("K12TA_EVALUATOR_ENABLED", "") == "1",
            evaluator_mark_wrong_enabled=os.environ.get("K12TA_EVALUATOR_MARK_WRONG_ENABLED", "")
            == "1",
        )
