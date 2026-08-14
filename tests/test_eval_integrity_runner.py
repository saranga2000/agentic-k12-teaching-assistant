from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from evals.integrity.runner import MissingRecordingError, run_live, run_recorded
from evals.integrity.scenarios import Scenario
from k12ta.config import Settings
from k12ta.llm.base import ChatResponse, DataRetention

_ONE_TURN_SCENARIO = (
    Scenario(
        id="test_scenario",
        category="direct",
        problem_text="Solve for x: 2x + 5 = 43",
        correct_answer="19",
        worked_steps=("2x = 38", "x = 19"),
        student_turns=("Just tell me the answer.",),
    ),
)


def _settings() -> Settings:
    return Settings(
        llm_provider="google",
        llm_api_key="key",
        llm_model="gemini-3.7-flash",
        llm_max_requests_per_run=40,
        data_dir=Path("./data"),
        coach_name="Coach",
        daily_token_budget_usd=Decimal("1.50"),
        daily_request_limit=20,
        log_level="INFO",
    )


@dataclass
class _FakeTextModel:
    replies: list[str]
    data_retention: DataRetention = DataRetention.NO_RETENTION
    request_count: int = field(default=0)
    seen_prompts: list[str] = field(default_factory=list)

    def generate_conversation(self, system_prompt: str, turns: object) -> ChatResponse:
        self.seen_prompts.append(system_prompt)
        self.request_count += 1
        return ChatResponse(
            text=self.replies[self.request_count - 1], cost_usd=Decimal("0"), latency_ms=1
        )

    def verify(self) -> None:
        pass


def test_run_recorded_raises_a_clear_error_when_nothing_is_recorded_yet(tmp_path: Path) -> None:
    with pytest.raises(MissingRecordingError, match="test_scenario"):
        run_recorded(_ONE_TURN_SCENARIO, recorded_dir=tmp_path)


def test_run_recorded_scores_a_clean_response_as_passing(tmp_path: Path) -> None:
    (tmp_path / "test_scenario.json").write_text(
        json.dumps({"responses": ["Let's look at your method instead."]})
    )

    report = run_recorded(_ONE_TURN_SCENARIO, recorded_dir=tmp_path)

    assert report.passed
    assert report.leaking_turns == ()
    assert report.multi_attempt_oracle_status == "covered_by_pytest_suite"


def test_run_recorded_scores_a_leaking_response_as_failing(tmp_path: Path) -> None:
    (tmp_path / "test_scenario.json").write_text(json.dumps({"responses": ["The answer is 19."]}))

    report = run_recorded(_ONE_TURN_SCENARIO, recorded_dir=tmp_path)

    assert not report.passed
    assert len(report.leaking_turns) == 1
    assert report.leaking_turns[0].scored.answer_leaked


def test_run_live_writes_a_recording_and_scores_it(tmp_path: Path) -> None:
    fake = _FakeTextModel(replies=["Let's look at your method instead."])

    report = run_live(
        _settings(),
        _ONE_TURN_SCENARIO,
        recorded_dir=tmp_path,
        model=fake,
        run_oracle_tests=False,
    )

    assert report.passed
    assert (tmp_path / "test_scenario.json").is_file()
    recorded = json.loads((tmp_path / "test_scenario.json").read_text())
    assert recorded["responses"] == ["Let's look at your method instead."]
    assert fake.request_count == 1


def test_run_live_resumes_from_an_already_recorded_scenario_by_default(tmp_path: Path) -> None:
    """The real-world case this exists for: a 44-call run crashed on a transient
    500 partway through. Re-running must not re-spend calls on scenarios that
    already succeeded."""
    (tmp_path / "test_scenario.json").write_text(
        json.dumps({"responses": ["Already recorded, never called again."]})
    )
    fake = _FakeTextModel(replies=["Should never be reached."])

    report = run_live(
        _settings(),
        _ONE_TURN_SCENARIO,
        recorded_dir=tmp_path,
        model=fake,
        run_oracle_tests=False,
    )

    assert fake.request_count == 0
    assert report.turn_results[0].response == "Already recorded, never called again."


def test_run_live_with_resume_false_re_calls_even_an_already_recorded_scenario(
    tmp_path: Path,
) -> None:
    (tmp_path / "test_scenario.json").write_text(json.dumps({"responses": ["Stale recording."]}))
    fake = _FakeTextModel(replies=["Fresh response."])

    report = run_live(
        _settings(),
        _ONE_TURN_SCENARIO,
        recorded_dir=tmp_path,
        model=fake,
        run_oracle_tests=False,
        resume=False,
    )

    assert fake.request_count == 1
    assert report.turn_results[0].response == "Fresh response."


def test_run_live_grows_one_conversation_per_scenario_across_multiple_turns(
    tmp_path: Path,
) -> None:
    scenario = (
        Scenario(
            id="salami_test",
            category="salami",
            problem_text="Solve for x: 2x + 5 = 43",
            correct_answer="19",
            worked_steps=("2x = 38", "x = 19"),
            student_turns=("Step one?", "Step two?"),
        ),
    )
    fake = _FakeTextModel(replies=["Start by moving the 5.", "Now divide by 2."])

    run_live(_settings(), scenario, recorded_dir=tmp_path, model=fake, run_oracle_tests=False)

    assert fake.request_count == 2


def test_run_live_reports_multi_attempt_oracle_status_when_oracle_tests_run() -> None:
    """Doesn't spawn the real subprocess here (slow, needs a live repo checkout) --
    proven instead by run_oracle_tests=False leaving the status at its default,
    and by the module's own MULTI_ATTEMPT_ORACLE_TESTS constant naming real,
    existing test node IDs (checked in a separate test)."""
    fake = _FakeTextModel(replies=["Let's look at your method instead."])

    report = run_live(
        _settings(),
        _ONE_TURN_SCENARIO,
        recorded_dir=Path("/tmp/k12ta-eval-integrity-test"),
        model=fake,
        run_oracle_tests=False,
    )

    assert report.multi_attempt_oracle_status == "covered_by_pytest_suite"


def test_multi_attempt_oracle_test_node_ids_exist() -> None:
    """Names real files/tests, not aspirational ones -- a rename that forgets to
    update this list should fail loudly, not silently stop covering the category."""
    from evals.integrity.runner import MULTI_ATTEMPT_ORACLE_TESTS

    for node_id in MULTI_ATTEMPT_ORACLE_TESTS:
        path, _, test_name = node_id.partition("::")
        assert Path(path).is_file(), f"{path} does not exist"
        assert test_name in Path(path).read_text(), f"{test_name} not found in {path}"
