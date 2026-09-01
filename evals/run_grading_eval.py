"""Score k12ta.grading.evaluator against the fixture corpus -- docs/EVALS.md
families 3 and 4. No test hits the network; running this script for real is
its own separate, deliberate step, never triggered automatically.

Family 3 (grading precision): precision of the evaluator's own INCORRECT
verdicts, per confidence band -- a false INCORRECT is the failure that costs
trust, so accuracy alone would hide it.

Family 4 (evaluator accuracy): overall agreement with a fixture's own
correct_answer, for both paths the evaluator actually has to judge on, always
reported separately, never pooled:
- keyed-mismatch: correct_answer used as the key (evaluate_keyed_mismatch),
  scored only on items where the deterministic exact-match already fails --
  the real population tier 2 ever sees, since tier 1 resolves the rest for
  free before the evaluator is ever called.
- keyless: correct_answer withheld entirely (evaluate_keyless), the same
  key-withheld method scripts/keyless_smoke_test.py already uses against
  live captures, generalised here to the fixture corpus. Its own two
  numbers -- answer-generation accuracy and verdict accuracy -- are kept
  apart from each other and from the keyed-mismatch scorecard.

Ground truth caveat, stated once here rather than repeated on every number:
whether a student's answer matches correct_answer is decided by this
module's own `_ground_truth`, a string/numeric proxy for correctness, not a
human semantic judgement. It is fully honest for numeric fixtures and exact
matches. For a genuinely semantic near-miss -- the exact case the evaluator
exists to judge, e.g. "rhombus" vs "quadrilateral" -- `grade_against_key`
alone would call it a plain INCORRECT (it has no notion of "might still be a
valid alternate name" -- see `k12ta.grading.needs_human.decide`, the only
other place that judgement is made, via `looks_numeric`), so `_ground_truth`
mirrors that same check and reports it as unscoreable instead of guessing
which way it should have gone. Every fixture item sliced this way is
counted and reported as `unscoreable`, never silently dropped.

Sliced by provenance (hand-labelled vs parent-correction, docs/EVALS.md
family 1) and by confidence band. Not sliced by answer shape or language --
docs/EVALS.md asks for both, but neither is a field the fixture schema
carries today (k12ta.evals.fixtures.FixturePage/FixtureItem), and this
script does not invent one to fill the gap.

    python evals/run_grading_eval.py
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from k12ta.config import Settings, load_dotenv
from k12ta.domain.models import GradeOutcome
from k12ta.evals.fixtures import FixturePage, load_fixture_pages
from k12ta.grading.evaluator import evaluate_keyed_mismatch, evaluate_keyless
from k12ta.grading.key_grader import grade_against_key, looks_numeric, normalise
from k12ta.llm import build_text_model
from k12ta.llm.base import MisconfiguredError, RateLimitExhaustedError, TextModel

FIXTURE_DIR = Path(__file__).parent / "fixtures"
RESULTS_DIR = Path(__file__).parent / "results"
CONFIDENCE_BANDS = [(0.95, 1.01), (0.85, 0.95), (0.7, 0.85), (0.0, 0.7)]


def _band_label(low: float, high: float) -> str:
    return f"{low:.2f}-{high:.2f}"


def _band_for(confidence: float) -> str:
    for low, high in CONFIDENCE_BANDS:
        if low <= confidence < high:
            return _band_label(low, high)
    return _band_label(*CONFIDENCE_BANDS[-1])


@dataclass
class VerdictScorecard:
    """One path's (keyed-mismatch or keyless) accumulated agreement counts,
    sliced by the evaluator's own confidence band. `unscoreable` items never
    reach any band -- see this module's own docstring for why."""

    items: int = 0
    unscoreable: int = 0
    band_totals: dict[str, int] = field(default_factory=dict)
    band_agree: dict[str, int] = field(default_factory=dict)
    band_incorrect_calls: dict[str, int] = field(default_factory=dict)
    band_incorrect_calls_true: dict[str, int] = field(default_factory=dict)

    def accuracy(self, band: str) -> float:
        total = self.band_totals.get(band, 0)
        return self.band_agree.get(band, 0) / total if total else 0.0

    def incorrect_precision(self, band: str) -> float:
        total = self.band_incorrect_calls.get(band, 0)
        return self.band_incorrect_calls_true.get(band, 0) / total if total else 0.0

    def record(
        self, evaluator_outcome: GradeOutcome, confidence: float, ground_truth: GradeOutcome
    ) -> None:
        self.items += 1
        band = _band_for(confidence)
        self.band_totals[band] = self.band_totals.get(band, 0) + 1
        if evaluator_outcome is ground_truth:
            self.band_agree[band] = self.band_agree.get(band, 0) + 1
        if evaluator_outcome is GradeOutcome.INCORRECT:
            self.band_incorrect_calls[band] = self.band_incorrect_calls.get(band, 0) + 1
            if ground_truth is GradeOutcome.INCORRECT:
                self.band_incorrect_calls_true[band] = (
                    self.band_incorrect_calls_true.get(band, 0) + 1
                )

    def render(self, title: str) -> str:
        lines = [
            f"### {title}",
            "",
            f"- items scored: {self.items}",
            f"- unscoreable (ground truth ambiguous from the string proxy): {self.unscoreable}",
        ]
        for band in sorted(self.band_totals, reverse=True):
            total = self.band_totals[band]
            lines.append(
                f"  - {band}: n={total} accuracy={self.accuracy(band):.3f} "
                f"incorrect-precision={self.incorrect_precision(band):.3f} "
                f"(incorrect calls: {self.band_incorrect_calls.get(band, 0)})"
            )
        return "\n".join(lines)


@dataclass
class GenerationScorecard:
    """Keyless-only: did the evaluator's own generated_answer match
    correct_answer, the answer it never saw -- family 4's other number, kept
    apart from verdict accuracy always."""

    attempted: int = 0
    matched: int = 0
    no_answer_generated: int = 0

    def rate(self) -> float:
        return self.matched / self.attempted if self.attempted else 0.0

    def render(self) -> str:
        return (
            f"- answer-generation accuracy: {self.rate():.3f} "
            f"({self.matched}/{self.attempted}, {self.no_answer_generated} produced no answer)"
        )


@dataclass
class EvalReport:
    keyed_mismatch: dict[str, VerdictScorecard]
    """Keyed by provenance."""
    keyless_verdict: dict[str, VerdictScorecard]
    keyless_generation: dict[str, GenerationScorecard]
    total_requests: int

    def to_markdown(self, model_name: str, run_at: datetime) -> str:
        lines = [
            f"# Grading precision eval (docs/EVALS.md families 3/4): {model_name}",
            "",
            f"Run at {run_at.isoformat(timespec='minutes')}.",
            f"Total API requests, including retries: {self.total_requests}.",
            "",
            "Ground truth is a string/numeric proxy (k12ta.grading.key_grader."
            "grade_against_key), not a human semantic judgement -- see this "
            "script's own module docstring before trusting any number below "
            "on a genuinely semantic near-miss.",
            "",
            "## Keyed-mismatch path (correct_answer used as the key)",
            "",
        ]
        for provenance in sorted(self.keyed_mismatch):
            lines.append(self.keyed_mismatch[provenance].render(f"provenance: {provenance}"))
            lines.append("")
        lines.append("## Keyless path (correct_answer withheld)")
        lines.append("")
        for provenance in sorted(self.keyless_verdict):
            lines.append(f"### provenance: {provenance}")
            lines.append("")
            lines.append(self.keyless_generation[provenance].render())
            lines.append("")
            lines.append(self.keyless_verdict[provenance].render("Verdict accuracy"))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _exact_match(student_answer: str, correct_answer: str) -> bool:
    return normalise(student_answer) == normalise(correct_answer)


def _ground_truth(student_answer: str, correct_answer: str) -> GradeOutcome:
    """grade_against_key alone cannot tell "confidently wrong" apart from
    "differs from a non-numeric key, might still be a valid alternate name"
    -- that extra judgement lives in k12ta.grading.needs_human.decide, not
    in grade_against_key itself, mirrored here rather than silently assumed:
    a numeric key has exactly one right value, so its INCORRECT is
    trustworthy ground truth; a non-numeric key's INCORRECT is exactly the
    semantic-near-miss case this evaluator exists to judge, so this reports
    it as NEEDS_HUMAN (unscoreable) instead of guessing which way it should
    have gone."""
    outcome = grade_against_key(student_answer, correct_answer, 1.0)
    if outcome is GradeOutcome.INCORRECT and not looks_numeric(correct_answer):
        return GradeOutcome.NEEDS_HUMAN
    return outcome


def score(text_model: TextModel, fixtures_dir: Path = FIXTURE_DIR) -> EvalReport:
    pages: list[FixturePage] = load_fixture_pages(fixtures_dir)
    keyed_mismatch: dict[str, VerdictScorecard] = {}
    keyless_verdict: dict[str, VerdictScorecard] = {}
    keyless_generation: dict[str, GenerationScorecard] = {}

    for page in pages:
        km_card = keyed_mismatch.setdefault(page.provenance, VerdictScorecard())
        kl_card = keyless_verdict.setdefault(page.provenance, VerdictScorecard())
        gen_card = keyless_generation.setdefault(page.provenance, GenerationScorecard())

        for item in page.items:
            ground_truth = _ground_truth(item.student_answer_raw, item.correct_answer)

            # Keyed-mismatch: only the population tier 2 would ever actually see --
            # tier 1 already resolves an exact match for free, before the evaluator
            # is ever called.
            if not _exact_match(item.student_answer_raw, item.correct_answer):
                if ground_truth is GradeOutcome.NEEDS_HUMAN:
                    km_card.unscoreable += 1
                else:
                    km_result = evaluate_keyed_mismatch(
                        text_model, item.prompt_text, item.student_answer_raw, item.correct_answer
                    )
                    km_card.record(km_result.outcome, km_result.confidence, ground_truth)

            # Keyless: every item, key withheld -- there is no deterministic
            # pre-filter in the live pipeline for a source with no key at all.
            gen_card.attempted += 1
            kl_result = evaluate_keyless(text_model, item.prompt_text, item.student_answer_raw)
            if kl_result.generated_answer is None:
                gen_card.no_answer_generated += 1
            elif normalise(kl_result.generated_answer) == normalise(item.correct_answer):
                gen_card.matched += 1
            if ground_truth is GradeOutcome.NEEDS_HUMAN:
                kl_card.unscoreable += 1
            else:
                kl_card.record(kl_result.outcome, kl_result.confidence, ground_truth)

    return EvalReport(
        keyed_mismatch=keyed_mismatch,
        keyless_verdict=keyless_verdict,
        keyless_generation=keyless_generation,
        total_requests=text_model.request_count,
    )


def _slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", text).strip("-") or "model"


def write_report(
    report: EvalReport,
    model_name: str,
    results_dir: Path = RESULTS_DIR,
    run_at: datetime | None = None,
) -> Path:
    run_at = run_at or datetime.now()
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = run_at.strftime("%Y-%m-%d-%H%M")
    report_path = results_dir / f"{stamp}-grading-eval-{_slugify(model_name)}.md"
    report_path.write_text(report.to_markdown(model_name, run_at))
    return report_path


def main() -> None:
    load_dotenv()
    pages = load_fixture_pages(FIXTURE_DIR)
    if not pages:
        print("No labelled fixtures found. See evals/fixtures/README.md.")
        return

    settings = Settings.from_env()
    text_model = build_text_model(settings)

    print("Preflight: verifying configured model and API key...", flush=True)
    try:
        text_model.verify()
    except (MisconfiguredError, RateLimitExhaustedError) as exc:
        print(f"Preflight failed, aborting before sending any page: {type(exc).__name__}: {exc}")
        return
    print("Preflight passed.", flush=True)

    report = score(text_model, FIXTURE_DIR)
    run_at = datetime.now()
    print(report.to_markdown(settings.llm_model, run_at))
    report_path = write_report(report, settings.llm_model, run_at=run_at)
    print(f"Report written to {report_path}")


if __name__ == "__main__":
    main()
