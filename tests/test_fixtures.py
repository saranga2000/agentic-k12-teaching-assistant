from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from k12ta.evals.fixtures import (
    CaptureMethod,
    FixtureValidationError,
    Layout,
    SpreadSide,
    load_fixture_pages,
)

VALID_PAGE: dict[str, object] = {
    "page_id": "2026-08-15-math-p12",
    "image": "pages/2026-08-15-math-p12.jpg",
    "source_id": "summer_bridge",
    "subject": "math",
    "capture_quality": "good",
    "capture_device": "ipad-air-m1",
    "capture_method": "camera-roll",
    "layout": "single-page",
    "items": [
        {
            "problem_id": "3",
            "prompt_text": "Solve for x: 3(x - 4) = 18",
            "student_answer_raw": "x = 2",
            "human_legible": True,
            "correct_answer": "x = 10",
        }
    ],
}


def _valid_page() -> dict[str, object]:
    return copy.deepcopy(VALID_PAGE)


def _write_label(tmp_path: Path, page: dict[str, object], name: str = "page.json") -> Path:
    label_path = tmp_path / name
    label_path.write_text(json.dumps(page))
    return label_path


def _touch_image(tmp_path: Path, relative: str) -> None:
    image_path = tmp_path / relative
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.touch()


def test_loads_a_valid_page(tmp_path: Path) -> None:
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    _write_label(tmp_path, page)

    pages = load_fixture_pages(tmp_path)

    assert len(pages) == 1
    loaded = pages[0]
    assert loaded.page_id == "2026-08-15-math-p12"
    assert loaded.capture_device == "ipad-air-m1"
    assert loaded.capture_method is CaptureMethod.CAMERA_ROLL
    assert loaded.layout is Layout.SINGLE_PAGE
    assert loaded.spread_side is None
    assert len(loaded.items) == 1
    assert loaded.items[0].problem_id == "3"
    assert loaded.items[0].correct_answer == "x = 10"


def test_loads_multiple_pages(tmp_path: Path) -> None:
    first = _valid_page()
    _touch_image(tmp_path, str(first["image"]))
    _write_label(tmp_path, first, "a.json")

    second = _valid_page()
    second["page_id"] = "second-page"
    second["image"] = "pages/second.jpg"
    _touch_image(tmp_path, str(second["image"]))
    _write_label(tmp_path, second, "b.json")

    pages = load_fixture_pages(tmp_path)

    assert {p.page_id for p in pages} == {"2026-08-15-math-p12", "second-page"}


def test_rejects_missing_image_path(tmp_path: Path) -> None:
    page = _valid_page()
    del page["image"]
    _write_label(tmp_path, page)

    with pytest.raises(FixtureValidationError, match="image"):
        load_fixture_pages(tmp_path)


def test_rejects_image_file_that_does_not_exist_on_disk(tmp_path: Path) -> None:
    page = _valid_page()
    # Deliberately do not create the image file.
    _write_label(tmp_path, page)

    with pytest.raises(FixtureValidationError, match="does not exist"):
        load_fixture_pages(tmp_path)


def test_rejects_duplicate_problem_id(tmp_path: Path) -> None:
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    items = page["items"]
    assert isinstance(items, list)
    items.append(copy.deepcopy(items[0]))
    _write_label(tmp_path, page)

    with pytest.raises(FixtureValidationError, match="duplicate"):
        load_fixture_pages(tmp_path)


def test_rejects_field_of_wrong_type(tmp_path: Path) -> None:
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    items = page["items"]
    assert isinstance(items, list)
    first_item = items[0]
    assert isinstance(first_item, dict)
    first_item["human_legible"] = "yes"
    _write_label(tmp_path, page)

    with pytest.raises(FixtureValidationError, match="human_legible"):
        load_fixture_pages(tmp_path)


def test_rejects_invalid_capture_method(tmp_path: Path) -> None:
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    page["capture_method"] = "mailed-in"
    _write_label(tmp_path, page)

    with pytest.raises(FixtureValidationError, match="capture_method"):
        load_fixture_pages(tmp_path)


def test_rejects_missing_capture_device(tmp_path: Path) -> None:
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    del page["capture_device"]
    _write_label(tmp_path, page)

    with pytest.raises(FixtureValidationError, match="capture_device"):
        load_fixture_pages(tmp_path)


def test_rejects_missing_capture_method(tmp_path: Path) -> None:
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    del page["capture_method"]
    _write_label(tmp_path, page)

    with pytest.raises(FixtureValidationError, match="capture_method"):
        load_fixture_pages(tmp_path)


@pytest.mark.parametrize(
    ("raw_device", "expected"),
    [
        ("Pixel 9a", "pixel-9a"),
        ("pixel-9a", "pixel-9a"),
        ("  iPad Air M1  ", "ipad-air-m1"),
        ("PIXEL   9A", "pixel-9a"),
    ],
)
def test_normalises_capture_device(tmp_path: Path, raw_device: str, expected: str) -> None:
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    page["capture_device"] = raw_device
    _write_label(tmp_path, page)

    pages = load_fixture_pages(tmp_path)

    assert pages[0].capture_device == expected


def test_rejects_missing_layout(tmp_path: Path) -> None:
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    del page["layout"]
    _write_label(tmp_path, page)

    with pytest.raises(FixtureValidationError, match="layout"):
        load_fixture_pages(tmp_path)


def test_rejects_invalid_layout(tmp_path: Path) -> None:
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    page["layout"] = "landscape"
    _write_label(tmp_path, page)

    with pytest.raises(FixtureValidationError, match="layout"):
        load_fixture_pages(tmp_path)


def test_loads_two_page_spread_with_spread_side(tmp_path: Path) -> None:
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    page["layout"] = "two-page-spread"
    page["spread_side"] = "left"
    _write_label(tmp_path, page)

    pages = load_fixture_pages(tmp_path)

    assert pages[0].layout is Layout.TWO_PAGE_SPREAD
    assert pages[0].spread_side is SpreadSide.LEFT


def test_rejects_two_page_spread_without_spread_side(tmp_path: Path) -> None:
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    page["layout"] = "two-page-spread"
    _write_label(tmp_path, page)

    with pytest.raises(FixtureValidationError, match="spread_side"):
        load_fixture_pages(tmp_path)


def test_rejects_invalid_spread_side(tmp_path: Path) -> None:
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    page["layout"] = "two-page-spread"
    page["spread_side"] = "middle"
    _write_label(tmp_path, page)

    with pytest.raises(FixtureValidationError, match="spread_side"):
        load_fixture_pages(tmp_path)


def test_rejects_spread_side_present_when_layout_is_single_page(tmp_path: Path) -> None:
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    page["spread_side"] = "left"
    _write_label(tmp_path, page)

    with pytest.raises(FixtureValidationError, match="spread_side"):
        load_fixture_pages(tmp_path)


def test_page_identity_defaults_to_empty_when_absent(tmp_path: Path) -> None:
    """Scope B: most fixtures predate this field entirely -- absence means "not
    labelled yet," not a validation error."""
    page = _valid_page()
    _touch_image(tmp_path, str(page["image"]))
    _write_label(tmp_path, page)

    pages = load_fixture_pages(tmp_path)

    assert pages[0].page_identity == {}


def test_loads_page_identity_candidates_per_kind(tmp_path: Path) -> None:
    page = _valid_page()
    page["page_identity"] = {
        "day_or_unit_banner": ["Day 1"],
        "printed_page_number": ["13"],
    }
    _touch_image(tmp_path, str(page["image"]))
    _write_label(tmp_path, page)

    pages = load_fixture_pages(tmp_path)

    assert pages[0].page_identity == {
        "day_or_unit_banner": ("Day 1",),
        "printed_page_number": ("13",),
    }


def test_page_identity_preserves_two_conflicting_values_on_a_spread(tmp_path: Path) -> None:
    """The real, common shape: a two-page spread showing two different "Day N"
    banners at once (7 of the real 9 Summer Bridge fixtures are exactly this) --
    ground truth for the CONFLICTING outcome, so both values must survive loading,
    never collapsed to one."""
    page = _valid_page()
    page["page_identity"] = {"day_or_unit_banner": ["Day 2", "Day 3"]}
    _touch_image(tmp_path, str(page["image"]))
    _write_label(tmp_path, page)

    pages = load_fixture_pages(tmp_path)

    assert pages[0].page_identity["day_or_unit_banner"] == ("Day 2", "Day 3")


def test_page_identity_accepts_any_component_name(tmp_path: Path) -> None:
    """Scope B's composite-schema rework: component names are open-ended,
    parent-defined per source (Summer Bridge's "section", RSM's "chapter"), not
    drawn from a fixed enum -- ground truth must accept whatever a real source's
    schema actually calls its markers."""
    page = _valid_page()
    page["page_identity"] = {"chapter_stamp": ["Ch. 3"]}
    _touch_image(tmp_path, str(page["image"]))
    _write_label(tmp_path, page)

    pages = load_fixture_pages(tmp_path)

    assert pages[0].page_identity == {"chapter_stamp": ("Ch. 3",)}


def test_rejects_page_identity_value_that_is_not_a_list_of_strings(tmp_path: Path) -> None:
    page = _valid_page()
    page["page_identity"] = {"day_or_unit_banner": "Day 1"}
    _touch_image(tmp_path, str(page["image"]))
    _write_label(tmp_path, page)

    with pytest.raises(FixtureValidationError, match="day_or_unit_banner"):
        load_fixture_pages(tmp_path)
