"""Run the M3.3 adversarial integrity eval.

    python -m evals.integrity.run          # replay evals/integrity/recorded/, free
    python -m evals.integrity.run --live   # real model calls, ~44 of them -- see
                                            # docs/EVALS.md section 2 for the cost.
                                            # Overwrites evals/integrity/recorded/
                                            # and writes a report to evals/results/.

Run as a module (`-m`), not as a script path -- this file imports its sibling
`evals.integrity.runner`, which needs the repo root on `sys.path`; `-m` does that
from the current working directory, a bare script path does not.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from evals.integrity.runner import EvalReport, MissingRecordingError, run_live, run_recorded
from evals.integrity.scenarios import total_turns
from k12ta.config import Settings, load_dotenv
from k12ta.llm.base import MisconfiguredError, RateLimitExhaustedError

RESULTS_DIR = Path(__file__).parent.parent / "results"


def _report_text(report: EvalReport) -> str:
    lines = [
        f"# Integrity eval — {'PASS' if report.passed else 'FAIL'}",
        "",
        f"Turns scored: {len(report.turn_results)}",
        f"Leaking turns: {len(report.leaking_turns)}",
        f"Consistency findings: {len(report.consistency_findings)}",
        f"Conversation-level findings: {len(report.conversation_findings)}",
        f"Multi-attempt oracle: {report.multi_attempt_oracle_status}",
        "",
        "## By category",
    ]
    for category, (leaking, total) in sorted(report.category_counts().items()):
        lines.append(f"- {category}: {total - leaking}/{total} clean")
    if report.leaking_turns:
        lines.append("")
        lines.append("## Leaks")
        for turn in report.leaking_turns:
            lines.append(
                f"- {turn.scenario_id} turn {turn.turn_index} ({turn.category}): "
                f"{turn.student_turn!r} -> {turn.response!r} "
                f"(answer_leaked={turn.scored.answer_leaked}, "
                f"worked_step_leaked={turn.scored.worked_step_leaked}, "
                f"confirmed_or_denied={turn.scored.confirmed_or_denied})"
            )
    if report.consistency_findings:
        lines.append("")
        lines.append("## Consistency findings")
        for finding in report.consistency_findings:
            lines.append(f"- {finding}")
    if report.conversation_findings:
        lines.append("")
        lines.append("## Conversation-level findings")
        for finding in report.conversation_findings:
            lines.append(f"- {finding}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="call the real model instead of replaying"
    )
    args = parser.parse_args()

    load_dotenv()
    settings = Settings.from_env()

    if not args.live:
        try:
            report = run_recorded()
        except MissingRecordingError as exc:
            print(str(exc))
            sys.exit(1)
        print(_report_text(report))
        sys.exit(0 if report.passed else 1)

    print(f"Live run: {total_turns()} real model calls against {settings.llm_model!r}.")
    from k12ta.llm import build_text_model

    model = build_text_model(settings)
    print("Preflight: verifying configured model and API key...", flush=True)
    try:
        model.verify()
    except (MisconfiguredError, RateLimitExhaustedError) as exc:
        print(f"Preflight failed, aborting before sending any turn: {type(exc).__name__}: {exc}")
        sys.exit(1)
    print("Preflight passed.", flush=True)

    report = run_live(settings)
    text = _report_text(report)
    print(text)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    report_path = RESULTS_DIR / f"{stamp}-integrity.md"
    report_path.write_text(text)
    print(f"Report written to {report_path}")
    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
