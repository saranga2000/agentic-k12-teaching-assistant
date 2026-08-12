from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from evals.run_transcription_eval import Scorecard, _normalise_prompt, score, write_report
from k12ta.llm.base import DataRetention
from k12ta.transcribe.base import TranscribedItem, TranscriptionResult


@dataclass
class FakeTranscriber:
    """Returns pre-canned results keyed by image path. Test-only, never shipped."""

    name: str
    responses: dict[str, TranscriptionResult]

    def transcribe(self, image_path: str) -> TranscriptionResult:
        return self.responses[image_path]


def _result(
    *items: TranscribedItem,
    data_retention: DataRetention = DataRetention.NO_RETENTION,
    failure: str | None = None,
) -> TranscriptionResult:
    return TranscriptionResult(
        items=items,
        provider="fake",
        model="fake",
        cost_usd=0.0,
        latency_ms=0,
        data_retention=data_retention,
        failure=failure,
    )


def _write_page(
    tmp_path: Path,
    page_id: str,
    image: str,
    capture_device: str,
    capture_method: str,
    items: list[dict[str, object]],
    layout: str = "single-page",
    spread_side: str | None = None,
    source_id: str = "summer_bridge",
) -> None:
    image_path = tmp_path / image
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.touch()
    page: dict[str, object] = {
        "page_id": page_id,
        "image": image,
        "source_id": source_id,
        "subject": "math",
        "capture_quality": "good",
        "capture_device": capture_device,
        "capture_method": capture_method,
        "layout": layout,
        "items": items,
    }
    if spread_side is not None:
        page["spread_side"] = spread_side
    (tmp_path / f"{page_id}.json").write_text(json.dumps(page))


def _item(problem_id: str, prompt_text: str, answer: str) -> dict[str, object]:
    return {
        "problem_id": problem_id,
        "prompt_text": prompt_text,
        "student_answer_raw": answer,
        "human_legible": True,
        "correct_answer": answer,
    }


def _two_page_fixture(tmp_path: Path) -> dict[str, str]:
    """Page A: ipad, camera-roll. Page B: pixel, app-ui. Returns image keys used by the fake."""
    _write_page(
        tmp_path,
        "page-a",
        "pages/a.jpg",
        "ipad-air-m1",
        "camera-roll",
        [
            _item("1", "What is 2+2?", "4"),
            _item("2", "What is 3+3?", "6"),
            _item("3", "What is 5+5?", "10"),
        ],
    )
    _write_page(
        tmp_path,
        "page-b",
        "pages/b.jpg",
        "pixel-9a",
        "app-ui",
        [
            _item("1", "Solve for x: x+1=2", "x = 1"),
            _item("2", "Solve for x: x+2=5", "x = 3"),
        ],
    )
    return {"a": str(tmp_path / "pages/a.jpg"), "b": str(tmp_path / "pages/b.jpg")}


def test_hand_computed_scores_across_two_pages(tmp_path: Path) -> None:
    keys = _two_page_fixture(tmp_path)
    transcriber = FakeTranscriber(
        name="fake",
        responses={
            keys["a"]: _result(
                TranscribedItem("1", "What is 2+2?", "4", confidence=0.97),  # exact match
                TranscribedItem("2", "What is 3+3?", "7", confidence=0.90),  # wrong answer
                # id "3" not returned at all: a genuine miss
                TranscribedItem("9", "What is 100/2?", "50", confidence=0.99),  # spurious
            ),
            keys["b"]: _result(
                # right problem, wrong printed number: misnumbered, not spurious/missed
                TranscribedItem("5", "Solve for x: x+1=2", "x=1", confidence=0.60),
                TranscribedItem("2", "Solve for x: x+2=5", "x = 3", confidence=0.40),  # exact
            ),
        },
    )

    report = score(transcriber, tmp_path)
    overall = report.overall

    assert overall.pages == 2
    assert overall.expected_items == 5
    assert overall.matched_items == 3
    assert overall.misnumbered_items == 1
    assert overall.spurious_items == 1
    assert overall.exact_matches == 2
    assert overall.detection_recall() == pytest.approx(4 / 5)
    assert overall.detection_precision() == pytest.approx(4 / 5)
    assert overall.exact_match_rate() == pytest.approx(2 / 3)
    assert overall.band_totals == {"0.95-1.01": 1, "0.85-0.95": 1, "0.00-0.85": 1}
    assert overall.band_correct == {"0.95-1.01": 1, "0.00-0.85": 1}

    ipad = report.by_device["ipad-air-m1"]
    assert (ipad.pages, ipad.expected_items, ipad.matched_items) == (1, 3, 2)
    assert ipad.misnumbered_items == 0
    assert ipad.spurious_items == 1
    assert ipad.detection_recall() == pytest.approx(2 / 3)
    assert ipad.exact_match_rate() == pytest.approx(1 / 2)

    pixel = report.by_device["pixel-9a"]
    assert (pixel.pages, pixel.expected_items, pixel.matched_items) == (1, 2, 1)
    assert pixel.misnumbered_items == 1
    assert pixel.spurious_items == 0
    assert pixel.detection_recall() == pytest.approx(1.0)
    assert pixel.exact_match_rate() == pytest.approx(1.0)

    camera_roll = report.by_method["camera-roll"]
    app_ui = report.by_method["app-ui"]
    assert (camera_roll.matched_items, camera_roll.spurious_items) == (2, 1)
    assert (app_ui.matched_items, app_ui.misnumbered_items) == (1, 1)

    # Slices must sum back to the overall scorecard.
    for field in (
        "expected_items",
        "matched_items",
        "misnumbered_items",
        "spurious_items",
        "unattributed_items",
    ):
        device_sum = sum(getattr(c, field) for c in report.by_device.values())
        method_sum = sum(getattr(c, field) for c in report.by_method.values())
        layout_sum = sum(getattr(c, field) for c in report.by_layout.values())
        source_sum = sum(getattr(c, field) for c in report.by_source.values())
        assert device_sum == getattr(overall, field)
        assert method_sum == getattr(overall, field)
        assert layout_sum == getattr(overall, field)
        assert source_sum == getattr(overall, field)


def test_slices_by_source_id(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "page-a",
        "pages/a.jpg",
        "ipad-air-m1",
        "camera-roll",
        [_item("1", "What is 2+2?", "4")],
        source_id="summer_bridge",
    )
    _write_page(
        tmp_path,
        "page-b",
        "pages/b.jpg",
        "ipad-air-m1",
        "camera-roll",
        [_item("2", "What is 3+3?", "6")],
        source_id="rsm",
    )
    keys = {
        "a": str(tmp_path / "pages/a.jpg"),
        "b": str(tmp_path / "pages/b.jpg"),
    }
    transcriber = FakeTranscriber(
        name="fake",
        responses={
            keys["a"]: _result(TranscribedItem("1", "What is 2+2?", "4", confidence=0.97)),
            keys["b"]: _result(TranscribedItem("2", "What is 3+3?", "wrong", confidence=0.97)),
        },
    )

    report = score(transcriber, tmp_path)

    bridge = report.by_source["summer_bridge"]
    rsm = report.by_source["rsm"]
    assert (bridge.pages, bridge.matched_items, bridge.exact_matches) == (1, 1, 1)
    assert (rsm.pages, rsm.matched_items, rsm.exact_matches) == (1, 1, 0)


def test_slices_by_layout_including_two_page_spread(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "page-a",
        "pages/a.jpg",
        "ipad-air-m1",
        "camera-roll",
        [_item("1", "What is 2+2?", "4")],
        layout="single-page",
    )
    _write_page(
        tmp_path,
        "page-b",
        "pages/b.jpg",
        "ipad-air-m1",
        "camera-roll",
        [_item("2", "What is 3+3?", "6")],
        layout="two-page-spread",
        spread_side="left",
    )
    keys = {
        "a": str(tmp_path / "pages/a.jpg"),
        "b": str(tmp_path / "pages/b.jpg"),
    }
    transcriber = FakeTranscriber(
        name="fake",
        responses={
            keys["a"]: _result(TranscribedItem("1", "What is 2+2?", "4", confidence=0.97)),
            keys["b"]: _result(TranscribedItem("2", "What is 3+3?", "wrong", confidence=0.97)),
        },
    )

    report = score(transcriber, tmp_path)

    single = report.by_layout["single-page"]
    spread = report.by_layout["two-page-spread"]
    assert (single.pages, single.matched_items, single.exact_matches) == (1, 1, 1)
    assert (spread.pages, spread.matched_items, spread.exact_matches) == (1, 1, 0)
    assert single.exact_match_rate() == pytest.approx(1.0)
    assert spread.exact_match_rate() == pytest.approx(0.0)


def test_two_page_spread_extra_detection_is_unattributed_not_spurious(tmp_path: Path) -> None:
    # Only the left page was labelled. The photo also shows the right page, so a
    # detection with no matching fixture item might be a hallucination or might be a
    # correct reading of the unlabelled right page — the fixture cannot tell us which,
    # so it must not be scored as a model failure either way.
    _write_page(
        tmp_path,
        "page-a",
        "pages/a.jpg",
        "ipad-air-m1",
        "camera-roll",
        [_item("1", "What is 2+2?", "4")],
        layout="two-page-spread",
        spread_side="left",
    )
    image_key = str(tmp_path / "pages/a.jpg")
    transcriber = FakeTranscriber(
        name="fake",
        responses={
            image_key: _result(
                TranscribedItem("1", "What is 2+2?", "4", confidence=0.99),
                TranscribedItem("9", "What is 9-4?", "5", confidence=0.95),
            )
        },
    )

    overall = score(transcriber, tmp_path).overall

    assert overall.matched_items == 1
    assert overall.spurious_items == 0
    assert overall.unattributed_items == 1
    assert overall.detection_recall() == pytest.approx(1.0)
    assert overall.detection_precision() == pytest.approx(1.0)


def test_misnumbered_item_excluded_from_spurious_missed_and_exact_match(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "page-a",
        "pages/a.jpg",
        "ipad-air-m1",
        "camera-roll",
        [_item("1", "What is 2+2?", "4")],
    )
    image_key = str(tmp_path / "pages/a.jpg")
    transcriber = FakeTranscriber(
        name="fake",
        # Same problem, wrong id, and an answer that would fail exact-match if it were scored.
        responses={
            image_key: _result(TranscribedItem("7", "What is 2+2?", "five", confidence=0.99))
        },
    )

    overall = score(transcriber, tmp_path).overall

    assert overall.matched_items == 0
    assert overall.misnumbered_items == 1
    assert overall.spurious_items == 0
    assert overall.exact_matches == 0
    assert overall.band_totals == {}
    assert overall.detection_recall() == pytest.approx(1.0)
    assert overall.detection_precision() == pytest.approx(1.0)
    assert overall.exact_match_rate() == 0.0


def test_spurious_item_not_explained_by_any_prompt(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "page-a",
        "pages/a.jpg",
        "ipad-air-m1",
        "camera-roll",
        [_item("1", "What is 2+2?", "4")],
    )
    image_key = str(tmp_path / "pages/a.jpg")
    transcriber = FakeTranscriber(
        name="fake",
        responses={
            image_key: _result(
                TranscribedItem("1", "What is 2+2?", "4", confidence=0.99),
                TranscribedItem("2", "What is the capital of France?", "Paris", confidence=0.9),
            )
        },
    )

    overall = score(transcriber, tmp_path).overall

    assert overall.matched_items == 1
    assert overall.misnumbered_items == 0
    assert overall.spurious_items == 1


def test_normalise_prompt_does_not_collide_different_problems() -> None:
    # key_grader.normalise strips all whitespace, so these two would collapse to the
    # same string under it ("whatis2+2?"). The prompt-text fallback must not do that.
    assert _normalise_prompt("What is 2+2?") != _normalise_prompt("What is 2 + 2?")


def test_normalise_prompt_collapses_whitespace_and_case_only() -> None:
    assert _normalise_prompt("  Solve for X:   3(x - 4) = 18  ") == "solve for x: 3(x - 4) = 18"


def test_empty_scorecard_rates_do_not_divide_by_zero() -> None:
    card = Scorecard()

    assert card.detection_recall() == 0.0
    assert card.detection_precision() == 0.0
    assert card.exact_match_rate() == 0.0
    assert card.calibration() == {}


def test_missing_item_with_no_transcription_lowers_recall_only(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "page-a",
        "pages/a.jpg",
        "ipad-air-m1",
        "camera-roll",
        [_item("1", "What is 2+2?", "4")],
    )
    image_key = str(tmp_path / "pages/a.jpg")
    transcriber = FakeTranscriber(name="fake", responses={image_key: _result()})

    overall = score(transcriber, tmp_path).overall

    assert overall.expected_items == 1
    assert overall.matched_items == 0
    assert overall.misnumbered_items == 0
    assert overall.spurious_items == 0
    assert overall.detection_recall() == 0.0
    assert overall.detection_precision() == 0.0


def test_score_reports_progress_per_page(tmp_path: Path) -> None:
    # A real transcriber can spend minutes per page retrying a rate limit, so progress
    # must be visible before and after each page, not only once the whole run ends.
    _write_page(
        tmp_path, "page-a", "pages/a.jpg", "ipad-air-m1", "camera-roll", [_item("1", "p", "a")]
    )
    _write_page(
        tmp_path, "page-b", "pages/b.jpg", "ipad-air-m1", "camera-roll", [_item("1", "p", "a")]
    )
    keys = {"a": str(tmp_path / "pages/a.jpg"), "b": str(tmp_path / "pages/b.jpg")}
    transcriber = FakeTranscriber(
        name="fake",
        responses={
            keys["a"]: _result(TranscribedItem("1", "p", "a", confidence=0.9)),
            keys["b"]: _result(failure="RuntimeError: network exploded"),
        },
    )
    messages: list[str] = []

    score(transcriber, tmp_path, on_progress=messages.append)

    assert messages == [
        "[1/2] page-a: transcribing...",
        "[1/2] page-a: scored",
        "[2/2] page-b: transcribing...",
        "[2/2] page-b: failed (RuntimeError: network exploded)",
    ]


def test_data_retention_is_surfaced_at_the_report_level(tmp_path: Path) -> None:
    _write_page(
        tmp_path, "page-a", "pages/a.jpg", "ipad-air-m1", "camera-roll", [_item("1", "p", "a")]
    )
    image_key = str(tmp_path / "pages/a.jpg")
    transcriber = FakeTranscriber(
        name="fake",
        responses={
            image_key: _result(
                TranscribedItem("1", "p", "a", confidence=0.9),
                data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            )
        },
    )

    report = score(transcriber, tmp_path)

    assert report.data_retention is DataRetention.PROVIDER_MAY_TRAIN


def test_markdown_header_states_data_retention_plainly(tmp_path: Path) -> None:
    _write_page(
        tmp_path, "page-a", "pages/a.jpg", "ipad-air-m1", "camera-roll", [_item("1", "p", "a")]
    )
    image_key = str(tmp_path / "pages/a.jpg")
    transcriber = FakeTranscriber(
        name="fake",
        responses={
            image_key: _result(
                TranscribedItem("1", "p", "a", confidence=0.9),
                data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            )
        },
    )
    report = score(transcriber, tmp_path)

    markdown = report.to_markdown("fake", datetime(2026, 8, 11, 10, 0))

    assert "provider_may_train" in markdown.lower()


def test_failed_page_is_excluded_from_scoring_and_reported_separately(tmp_path: Path) -> None:
    _write_page(
        tmp_path, "page-a", "pages/a.jpg", "ipad-air-m1", "camera-roll", [_item("1", "p", "a")]
    )
    _write_page(
        tmp_path, "page-b", "pages/b.jpg", "ipad-air-m1", "camera-roll", [_item("1", "p", "a")]
    )
    keys = {"a": str(tmp_path / "pages/a.jpg"), "b": str(tmp_path / "pages/b.jpg")}
    transcriber = FakeTranscriber(
        name="fake",
        responses={
            keys["a"]: _result(TranscribedItem("1", "p", "a", confidence=0.9)),
            keys["b"]: _result(failure="RuntimeError: network exploded"),
        },
    )

    report = score(transcriber, tmp_path)

    assert report.overall.pages == 1
    assert report.overall.expected_items == 1
    assert len(report.failed_pages) == 1
    assert report.failed_pages[0].page_id == "page-b"
    assert "network exploded" in report.failed_pages[0].reason


def test_markdown_lists_failed_pages_separately_from_zero_scores(tmp_path: Path) -> None:
    _write_page(
        tmp_path, "page-a", "pages/a.jpg", "ipad-air-m1", "camera-roll", [_item("1", "p", "a")]
    )
    image_key = str(tmp_path / "pages/a.jpg")
    transcriber = FakeTranscriber(
        name="fake", responses={image_key: _result(failure="RuntimeError: boom")}
    )

    report = score(transcriber, tmp_path)
    markdown = report.to_markdown("fake", datetime(2026, 8, 11, 10, 0))

    assert "page-a" in markdown
    assert "boom" in markdown
    assert report.overall.pages == 0
    assert report.overall.expected_items == 0


def test_write_report_preserves_same_day_reruns(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "page-a",
        "pages/a.jpg",
        "ipad-air-m1",
        "camera-roll",
        [_item("1", "What is 2+2?", "4")],
    )
    image_key = str(tmp_path / "pages/a.jpg")
    transcriber = FakeTranscriber(
        name="vision test/v2",
        responses={image_key: _result(TranscribedItem("1", "What is 2+2?", "4", confidence=0.97))},
    )
    report = score(transcriber, tmp_path)
    results_dir = tmp_path / "results"

    first_at = datetime(2026, 8, 11, 9, 5)
    second_at = datetime(2026, 8, 11, 14, 30)
    first = write_report(report, transcriber.name, results_dir, run_at=first_at)
    second = write_report(report, transcriber.name, results_dir, run_at=second_at)

    assert first != second
    assert first.name == "2026-08-11-0905-vision-test-v2.md"
    assert second.name == "2026-08-11-1430-vision-test-v2.md"
    assert first.exists() and second.exists()
    assert "detection recall" in first.read_text().lower()
