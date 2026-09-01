"""evals/run_grading_eval.py: docs/EVALS.md families 3/4, scored against the
fixture corpus. No test hits the network -- every model call goes through
tests.fakes.FakeTextModel.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.run_grading_eval import score
from tests.fakes import FakeTextModel


def _write_page(
    tmp_path: Path,
    page_id: str,
    items: list[dict[str, object]],
    provenance: str | None = None,
    source_id: str = "summer_bridge",
) -> None:
    image = f"{page_id}.jpg"
    image_path = tmp_path / image
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.touch()
    page: dict[str, object] = {
        "page_id": page_id,
        "image": image,
        "source_id": source_id,
        "subject": "math",
        "capture_quality": "good",
        "capture_device": "ipad-air-m1",
        "capture_method": "camera-roll",
        "layout": "single-page",
        "items": items,
    }
    if provenance is not None:
        page["provenance"] = provenance
    (tmp_path / f"{page_id}.json").write_text(json.dumps(page))


def _item(
    problem_id: str, prompt_text: str, student_answer: str, correct_answer: str
) -> dict[str, object]:
    return {
        "problem_id": problem_id,
        "prompt_text": prompt_text,
        "student_answer_raw": student_answer,
        "human_legible": True,
        "correct_answer": correct_answer,
    }


def _reply(verdict: str, confidence: float, generated_answer: str | None = None) -> str:
    return json.dumps(
        {"verdict": verdict, "confidence": confidence, "generated_answer": generated_answer}
    )


def test_exact_match_is_excluded_from_keyed_mismatch_but_scored_keyless(tmp_path: Path) -> None:
    """Tier 1 would resolve an exact match for free -- the evaluator never
    sees it live, so the keyed-mismatch scorecard must not either. Keyless
    has no such pre-filter (no key to compare against deterministically)."""
    _write_page(tmp_path, "p1", [_item("1", "2 + 2", "4", "4")])
    text_model = FakeTextModel(
        replies=[
            _reply("correct", 0.9, generated_answer="4"),
            _reply("correct", 0.9, generated_answer="4"),
        ]
    )

    report = score(text_model, tmp_path)

    km_card = report.keyed_mismatch["hand-labelled"]
    assert km_card.items == 0
    assert km_card.unscoreable == 0
    kl_card = report.keyless_verdict["hand-labelled"]
    assert kl_card.items == 1
    gen_card = report.keyless_generation["hand-labelled"]
    assert gen_card.attempted == 1
    assert gen_card.matched == 1


def test_numeric_mismatch_is_scored_on_the_keyed_mismatch_path(tmp_path: Path) -> None:
    _write_page(tmp_path, "p1", [_item("1", "2 + 2", "5", "4")])
    text_model = FakeTextModel(
        replies=[
            _reply("incorrect", 0.9),  # keyed-mismatch call
            _reply("incorrect", 0.9, generated_answer="4"),  # keyless call 1
            _reply("incorrect", 0.9, generated_answer="4"),  # keyless call 2
        ]
    )

    report = score(text_model, tmp_path)

    km_card = report.keyed_mismatch["hand-labelled"]
    assert km_card.items == 1
    assert km_card.unscoreable == 0
    assert km_card.accuracy("0.85-0.95") == 1.0
    assert km_card.incorrect_precision("0.85-0.95") == 1.0


def test_semantic_near_miss_is_unscoreable_on_both_paths(tmp_path: Path) -> None:
    """grade_against_key alone would call this a plain INCORRECT (it has no
    notion of "might still be a valid alternate name"), but the key is
    non-numeric, so evals.run_grading_eval._ground_truth reclassifies it as
    unscoreable -- this script must not guess which way a semantic near-miss
    should have gone, on either path."""
    _write_page(tmp_path, "p1", [_item("1", "shape?", "rhombus", "quadrilateral")])
    text_model = FakeTextModel(
        replies=[
            _reply("correct", 0.9),  # keyed-mismatch call (semantic judgement)
            _reply("needs_human", 0.0),  # keyless call 1
            _reply("needs_human", 0.0),  # keyless call 2
        ]
    )

    report = score(text_model, tmp_path)

    km_card = report.keyed_mismatch["hand-labelled"]
    assert km_card.items == 0
    assert km_card.unscoreable == 1
    kl_card = report.keyless_verdict["hand-labelled"]
    assert kl_card.items == 0
    assert kl_card.unscoreable == 1
    # Generation accuracy still runs regardless -- it doesn't use grade_against_key.
    gen_card = report.keyless_generation["hand-labelled"]
    assert gen_card.attempted == 1


def test_generation_accuracy_counts_answers_that_never_arrived(tmp_path: Path) -> None:
    _write_page(tmp_path, "p1", [_item("1", "2 + 2", "5", "4")])
    text_model = FakeTextModel(
        replies=[
            _reply("incorrect", 0.9),
            _reply("needs_human", 0.0, generated_answer=None),
            _reply("needs_human", 0.0, generated_answer=None),
        ]
    )

    report = score(text_model, tmp_path)

    gen_card = report.keyless_generation["hand-labelled"]
    assert gen_card.attempted == 1
    assert gen_card.matched == 0
    assert gen_card.no_answer_generated == 1


def test_provenance_slices_are_kept_separate(tmp_path: Path) -> None:
    _write_page(tmp_path, "p1", [_item("1", "2 + 2", "5", "4")], provenance="hand-labelled")
    _write_page(tmp_path, "p2", [_item("1", "3 + 3", "7", "6")], provenance="parent-correction")
    text_model = FakeTextModel(
        replies=[_reply("incorrect", 0.9)] * 2
        + [
            _reply("incorrect", 0.9, generated_answer="4"),
            _reply("incorrect", 0.9, generated_answer="4"),
        ]
        + [
            _reply("incorrect", 0.9, generated_answer="6"),
            _reply("incorrect", 0.9, generated_answer="6"),
        ]
    )

    report = score(text_model, tmp_path)

    assert report.keyed_mismatch["hand-labelled"].items == 1
    assert report.keyed_mismatch["parent-correction"].items == 1
    assert set(report.keyless_verdict) == {"hand-labelled", "parent-correction"}


def test_incorrect_precision_reflects_a_false_incorrect(tmp_path: Path) -> None:
    """The one number family 3 cares most about: a confidently wrong
    INCORRECT call must show up as a precision hit, not vanish into an
    overall accuracy average."""
    _write_page(
        tmp_path,
        "p1",
        [
            _item("1", "2 + 2", "5", "4"),  # genuinely incorrect (numeric mismatch)
            # "7 units" vs "7": numeric_part matches both to "7" (unit ignored,
            # k12ta.grading.key_grader.numeric_part), so grade_against_key's
            # ground truth is CORRECT -- but the plain string differs, so
            # _exact_match does NOT skip this from keyed-mismatch scoring.
            _item("2", "10 - 3", "7 units", "7"),
        ],
    )
    text_model = FakeTextModel(
        replies=[
            _reply("incorrect", 0.9),  # km q1: correctly incorrect
            _reply("incorrect", 0.9, generated_answer="4"),  # kl q1 call 1
            _reply("incorrect", 0.9, generated_answer="4"),  # kl q1 call 2
            _reply("incorrect", 0.9),  # km q2: FALSELY incorrect -- ground truth is correct
            _reply("correct", 0.9, generated_answer="7"),  # kl q2 call 1
            _reply("correct", 0.9, generated_answer="7"),  # kl q2 call 2
        ]
    )

    report = score(text_model, tmp_path)

    km_card = report.keyed_mismatch["hand-labelled"]
    assert km_card.items == 2
    assert km_card.band_incorrect_calls["0.85-0.95"] == 2
    assert km_card.band_incorrect_calls_true["0.85-0.95"] == 1
    assert km_card.incorrect_precision("0.85-0.95") == 0.5


def test_total_requests_reflects_every_call_made(tmp_path: Path) -> None:
    _write_page(tmp_path, "p1", [_item("1", "2 + 2", "5", "4")])
    text_model = FakeTextModel(
        replies=[
            _reply("incorrect", 0.9),
            _reply("incorrect", 0.9, generated_answer="4"),
            _reply("incorrect", 0.9, generated_answer="4"),
        ]
    )

    report = score(text_model, tmp_path)

    assert report.total_requests == 3  # 1 keyed-mismatch call + 2 keyless calls


def test_report_renders_to_markdown_without_crashing(tmp_path: Path) -> None:
    _write_page(tmp_path, "p1", [_item("1", "2 + 2", "5", "4")])
    text_model = FakeTextModel(
        replies=[
            _reply("incorrect", 0.9),
            _reply("incorrect", 0.9, generated_answer="4"),
            _reply("incorrect", 0.9, generated_answer="4"),
        ]
    )

    report = score(text_model, tmp_path)
    from datetime import datetime

    markdown = report.to_markdown("fake-model", datetime.now())

    assert "Keyed-mismatch path" in markdown
    assert "Keyless path" in markdown
    assert "hand-labelled" in markdown
