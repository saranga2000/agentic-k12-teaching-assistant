"""docs/ROADMAP.md's M5 "fixture promotion": turning a parent's verdict
correction into one more fixture in k12ta.evals.fixtures's own schema, tagged
provenance="parent-correction" (docs/EVALS.md family 1). No network, no
image decoding -- write_correction_fixture only ever copies bytes.
"""

from __future__ import annotations

from pathlib import Path

from k12ta.evals.fixtures import (
    CaptureMethod,
    FixtureProvenance,
    Layout,
    build_correction_fixture_item,
    load_fixture_pages,
    promote_correction,
    write_correction_fixture,
)


def test_build_item_uses_the_key_answer_when_one_exists() -> None:
    item = build_correction_fixture_item(
        problem_id="1",
        prompt_text="shape?",
        student_answer_raw="rhombus",
        expected_answer="quadrilateral",
        new_outcome="correct",
    )

    assert item is not None
    assert item.correct_answer == "quadrilateral"
    assert item.human_legible is True


def test_build_item_uses_the_key_answer_even_when_marked_incorrect() -> None:
    """A keyed mismatch a parent judged still has the key's own text to fall
    back on as ground truth, regardless of which way the verdict went."""
    item = build_correction_fixture_item(
        problem_id="1",
        prompt_text="shape?",
        student_answer_raw="triangle",
        expected_answer="quadrilateral",
        new_outcome="incorrect",
    )

    assert item is not None
    assert item.correct_answer == "quadrilateral"


def test_build_item_uses_the_students_own_answer_when_marked_correct_with_no_key() -> None:
    item = build_correction_fixture_item(
        problem_id="4",
        prompt_text="describe the pattern",
        student_answer_raw="it doubles each time",
        expected_answer=None,
        new_outcome="correct",
    )

    assert item is not None
    assert item.correct_answer == "it doubles each time"


def test_build_item_returns_none_when_incorrect_with_no_known_right_answer() -> None:
    """Nothing here names what's actually right -- promoting a fixture would
    mean fabricating a ground truth, which this never does."""
    item = build_correction_fixture_item(
        problem_id="4",
        prompt_text="describe the pattern",
        student_answer_raw="it stays the same",
        expected_answer=None,
        new_outcome="incorrect",
    )

    assert item is None


def test_build_item_returns_none_for_partially_correct_with_no_known_right_answer() -> None:
    item = build_correction_fixture_item(
        problem_id="4",
        prompt_text="describe the pattern",
        student_answer_raw="half of it is right",
        expected_answer=None,
        new_outcome="partially_correct",
    )

    assert item is None


def test_write_correction_fixture_copies_the_image_and_writes_a_valid_label(
    tmp_path: Path,
) -> None:
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    source_image = tmp_path / "capture.jpg"
    source_image.write_bytes(b"not a real jpeg, just bytes")
    item = build_correction_fixture_item(
        problem_id="1",
        prompt_text="12 + 7",
        student_answer_raw="19",
        expected_answer=None,
        new_outcome="correct",
    )
    assert item is not None

    label_path = write_correction_fixture(
        fixtures_dir,
        page_id="summer_bridge-17-c1",
        source_id="summer_bridge",
        subject="math",
        image_source_path=source_image,
        item=item,
    )

    assert label_path == fixtures_dir / "summer_bridge-17-c1.json"
    copied_image = fixtures_dir / "pages" / "summer_bridge-17-c1.jpg"
    assert copied_image.read_bytes() == b"not a real jpeg, just bytes"

    pages = load_fixture_pages(fixtures_dir)
    assert len(pages) == 1
    page = pages[0]
    assert page.provenance == FixtureProvenance.PARENT_CORRECTION
    assert page.source_id == "summer_bridge"
    assert page.subject == "math"
    assert page.capture_method is CaptureMethod.APP_UI
    assert page.layout is Layout.SINGLE_PAGE
    assert page.spread_side is None
    assert page.capture_quality is None
    assert page.capture_device is None
    assert len(page.items) == 1
    assert page.items[0].correct_answer == "19"


def test_write_correction_fixture_overwrites_a_previous_promotion_of_the_same_page(
    tmp_path: Path,
) -> None:
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    source_image = tmp_path / "capture.jpg"
    source_image.write_bytes(b"first")
    first_item = build_correction_fixture_item(
        problem_id="1",
        prompt_text="12 + 7",
        student_answer_raw="18",
        expected_answer=None,
        new_outcome="correct",
    )
    assert first_item is not None
    write_correction_fixture(
        fixtures_dir,
        page_id="p1",
        source_id="summer_bridge",
        subject="math",
        image_source_path=source_image,
        item=first_item,
    )

    second_image = tmp_path / "capture2.jpg"
    second_image.write_bytes(b"second")
    second_item = build_correction_fixture_item(
        problem_id="1",
        prompt_text="12 + 7",
        student_answer_raw="19",
        expected_answer=None,
        new_outcome="correct",
    )
    assert second_item is not None
    write_correction_fixture(
        fixtures_dir,
        page_id="p1",
        source_id="summer_bridge",
        subject="math",
        image_source_path=second_image,
        item=second_item,
    )

    pages = load_fixture_pages(fixtures_dir)
    assert len(pages) == 1
    assert pages[0].items[0].correct_answer == "19"


def test_promote_correction_returns_none_and_writes_nothing_when_unpromotable(
    tmp_path: Path,
) -> None:
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    source_image = tmp_path / "capture.jpg"
    source_image.write_bytes(b"bytes")

    result = promote_correction(
        fixtures_dir,
        page_id="p1",
        source_id="summer_bridge",
        subject="math",
        image_source_path=source_image,
        problem_id="4",
        prompt_text="describe the pattern",
        student_answer_raw="it stays the same",
        expected_answer=None,
        new_outcome="incorrect",
    )

    assert result is None
    assert list(fixtures_dir.glob("*.json")) == []


def test_promote_correction_writes_a_fixture_when_promotable(tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    source_image = tmp_path / "capture.jpg"
    source_image.write_bytes(b"bytes")

    result = promote_correction(
        fixtures_dir,
        page_id="p1",
        source_id="summer_bridge",
        subject="math",
        image_source_path=source_image,
        problem_id="1",
        prompt_text="shape?",
        student_answer_raw="rhombus",
        expected_answer="quadrilateral",
        new_outcome="correct",
    )

    assert result == fixtures_dir / "p1.json"
    pages = load_fixture_pages(fixtures_dir)
    assert pages[0].items[0].correct_answer == "quadrilateral"
