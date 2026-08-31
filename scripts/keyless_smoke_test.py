"""M6's key-withheld smoke test (docs/EVALS.md family 4, docs/ROADMAP.md's M6).

Summer Bridge already has real confirmed answer keys and real graded child
captures on disk. This withholds the key from the evaluator, runs the keyless
path (k12ta.grading.evaluator.evaluate_keyless) against the child's already-
transcribed answer, then scores the result against the key it never saw.

Read-only against the real household database -- opened via a sqlite3 URI in
explicit ro mode, a hard technical guarantee on top of the fact that nothing
in this script ever calls an insert/update function. No new photographs, no
labelling session: everything needed is already on file.

A SMOKE TEST, not a calibration number. Five pages cannot produce precision
per confidence band, which is what M6's real "done when" requires -- this
only checks that the mechanism runs against real material without crashing,
and gives a first, honest read on two separate numbers. Summer Bridge is not
the hard case (RSM Pre-Algebra Advanced is); a good number here says nothing
about RSM.

Hard caps, enforced in code, not left as a convention:
- MAX_PAGES = 5. One problem evaluated per page (not every problem on it),
  so the actual live-call count stays exactly bounded, not a function of how
  many problems a page happens to have.
- MAX_REQUESTS = 10 (5 pages x 2 calls each, evaluate_keyless's own
  independent-solve-plus-cross-check shape). Checked before every call, not
  after -- a call that would exceed it is never made.
- No automatic retry into a failure this script doesn't understand. Any
  exception stops the run immediately; whatever completed before it is
  still scored and written, clearly marked as a partial run.

    K12TA_LLM_API_KEY=... python scripts/keyless_smoke_test.py
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from k12ta.config import Settings, load_dotenv
from k12ta.grading.evaluator import EvaluatorResult, evaluate_keyless
from k12ta.grading.key_grader import find_key_entry, normalise
from k12ta.llm import build_text_model
from k12ta.store import answer_keys

MAX_PAGES = 5
MAX_REQUESTS = 10
SOURCE_ID = "summer_bridge"

RESULTS_DIR = Path(__file__).resolve().parents[1] / "evals" / "results"


@dataclass(frozen=True)
class SmokePage:
    student_id: str
    page_number: int
    problem_id: str
    prompt_text: str
    student_answer_raw: str
    key_answer_text: str
    real_outcome: str
    """The already-graded, key-based outcome on file -- the ground truth for
    verdict accuracy, computed by the deterministic path long before this
    script ever ran."""


@dataclass(frozen=True)
class SmokeResult:
    page: SmokePage
    evaluator: EvaluatorResult
    answer_generation_correct: bool
    verdict_correct: bool


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _select_pages(conn: sqlite3.Connection) -> list[SmokePage]:
    """Up to MAX_PAGES distinct Summer Bridge pages with both a real,
    already-confirmed answer key and a real, already-graded child capture on
    file. One problem per page, chosen deterministically (lowest problem_id),
    so the request count this script spends is exactly bounded by MAX_PAGES,
    never by how many problems happen to be on a page."""
    page_rows = conn.execute(
        """
        SELECT DISTINCT gp.student_id, gp.page_number
        FROM graded_problems gp
        JOIN page_captures pc
            ON pc.student_id = gp.student_id AND pc.capture_id = gp.capture_id
        JOIN assignments a
            ON a.student_id = pc.student_id AND a.assignment_id = pc.assignment_id
        WHERE a.source_id = ? AND gp.outcome IN ('correct', 'incorrect')
            AND gp.page_number IS NOT NULL
        ORDER BY gp.page_number
        """,
        (SOURCE_ID,),
    ).fetchall()

    pages: list[SmokePage] = []
    for row in page_rows:
        if len(pages) >= MAX_PAGES:
            break
        student_id, page_number = row["student_id"], row["page_number"]
        problem_row = conn.execute(
            """
            SELECT gp.problem_id, gp.capture_id, gp.outcome
            FROM graded_problems gp
            JOIN page_captures pc
                ON pc.student_id = gp.student_id AND pc.capture_id = gp.capture_id
            JOIN assignments a
                ON a.student_id = pc.student_id AND a.assignment_id = pc.assignment_id
            WHERE a.source_id = ? AND gp.student_id = ? AND gp.page_number = ?
                AND gp.outcome IN ('correct', 'incorrect')
            ORDER BY gp.problem_id ASC
            LIMIT 1
            """,
            (SOURCE_ID, student_id, page_number),
        ).fetchone()
        if problem_row is None:
            continue
        problem = conn.execute(
            """
            SELECT prompt_text, student_answer_raw FROM problems
            WHERE student_id = ? AND capture_id = ? AND problem_id = ?
            """,
            (student_id, problem_row["capture_id"], problem_row["problem_id"]),
        ).fetchone()
        if problem is None:
            continue
        entries = answer_keys.get_entries_for_page(conn, student_id, SOURCE_ID, page_number)
        key_entry = find_key_entry(entries, problem_row["problem_id"])
        if key_entry is None or key_entry.answer_text is None:
            continue
        pages.append(
            SmokePage(
                student_id=student_id,
                page_number=page_number,
                problem_id=problem_row["problem_id"],
                prompt_text=problem["prompt_text"],
                student_answer_raw=problem["student_answer_raw"],
                key_answer_text=key_entry.answer_text,
                real_outcome=problem_row["outcome"],
            )
        )
    return pages


def run() -> tuple[list[SmokeResult], list[SmokePage], str | None]:
    """Returns (completed results, pages selected but not yet run, an error
    message if the run stopped early -- None on a clean, complete run)."""
    load_dotenv()
    settings = Settings.from_env()
    conn = _connect_readonly(settings.data_dir / "k12ta.db")
    try:
        pages = _select_pages(conn)
    finally:
        conn.close()

    if not pages:
        return (
            [],
            [],
            "No eligible Summer Bridge pages found "
            "(need both a confirmed key and a graded child capture).",
        )

    text_model = build_text_model(settings)
    results: list[SmokeResult] = []
    for page in pages:
        if text_model.request_count + 2 > MAX_REQUESTS:
            return (
                results,
                pages[len(results) :],
                (
                    f"Stopped before page {page.page_number}: the next call would exceed "
                    f"MAX_REQUESTS={MAX_REQUESTS} (spent {text_model.request_count} so far)."
                ),
            )
        print(
            f"[{text_model.request_count}/{MAX_REQUESTS} requests] "
            f"evaluating {page.student_id} page {page.page_number} problem {page.problem_id}..."
        )
        try:
            evaluator_result = evaluate_keyless(
                text_model, page.prompt_text, page.student_answer_raw
            )
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: stop on anything, no retry
            reason = f"{type(exc).__name__}: {exc}"
            print(f"STOPPED, no retry: {reason}")
            return results, pages[len(results) :], reason
        print(
            f"  -> requests now {text_model.request_count}, "
            f"verdict={evaluator_result.outcome.value}, "
            f"confidence={evaluator_result.confidence}, "
            f"generated_answer={evaluator_result.generated_answer!r}"
        )
        answer_generation_correct = evaluator_result.generated_answer is not None and normalise(
            evaluator_result.generated_answer
        ) == normalise(page.key_answer_text)
        verdict_correct = evaluator_result.outcome.value == page.real_outcome
        results.append(
            SmokeResult(
                page=page,
                evaluator=evaluator_result,
                answer_generation_correct=answer_generation_correct,
                verdict_correct=verdict_correct,
            )
        )
    return results, [], None


def _write_report(
    results: list[SmokeResult], remaining: list[SmokePage], error: str | None
) -> Path:
    now = datetime.now(UTC)
    path = RESULTS_DIR / f"{now.strftime('%Y-%m-%d-%H%M')}-keyless-smoke.md"
    lines = [
        "# Keyless evaluator smoke test -- NOT a calibration number",
        "",
        "docs/EVALS.md family 4's key-withheld method, run against Summer Bridge (the",
        "easy case -- RSM Pre-Algebra Advanced is where this path is actually exposed;",
        "a good number here says nothing about whether the keyless path is safe there).",
        "Five pages cannot produce precision per confidence band, which is what M6's",
        'real "done when" requires. This is a smoke test: does the mechanism run against',
        "real material without crashing, and a first, honest read on two numbers that",
        "must never be merged.",
        "",
        f"Pages selected: {len(results) + len(remaining)}. Completed: {len(results)}."
        + (f" Stopped early: {error}" if error else ""),
        "",
    ]
    if results:
        gen_correct = sum(1 for r in results if r.answer_generation_correct)
        verdict_correct = sum(1 for r in results if r.verdict_correct)
        lines += [
            f"**Answer-generation accuracy: {gen_correct}/{len(results)}** -- did the agent's own",
            "generated answer match the key it never saw?",
            "",
            f"**Verdict accuracy: {verdict_correct}/{len(results)}** -- did the agent's",
            "verdict on the child's real answer match the key-based grade already on file?",
            "",
            "## Per page",
            "",
        ]
        for r in results:
            lines.append(
                f"- {r.page.student_id} page {r.page.page_number} problem {r.page.problem_id}: "
                f"key={r.page.key_answer_text!r}, child wrote={r.page.student_answer_raw!r}, "
                f"real outcome={r.page.real_outcome}, "
                f"agent generated={r.evaluator.generated_answer!r} "
                f"(match: {r.answer_generation_correct}), "
                f"agent verdict={r.evaluator.outcome.value} "
                f"(match: {r.verdict_correct}), "
                f"confidence={r.evaluator.confidence}"
            )
        lines.append("")
    if remaining:
        lines.append(f"Not reached: {len(remaining)} page(s) selected but not run before the stop.")
        lines.append("")
    lines.append(
        "**Do not read this as evidence the keyless path is safe for RSM.** Summer Bridge"
        " has clean, well-structured printed answers; RSM Pre-Algebra Advanced is where"
        " this path's real exposure is."
    )
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def main() -> None:
    results, remaining, error = run()
    path = _write_report(results, remaining, error)
    print(f"\nWrote {path}")
    if error:
        print(f"Run stopped early: {error}")


if __name__ == "__main__":
    main()
