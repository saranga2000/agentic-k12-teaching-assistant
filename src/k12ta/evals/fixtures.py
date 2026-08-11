"""Fixture schema and loader for hand-labelled evaluation pages.

Validates each label file so a malformed fixture fails at load time, not silently
inside a scoring run days later. Does not read image bytes or call a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class CaptureMethod(Enum):
    """How a labelled page's image reached evals/fixtures/pages/."""

    CAMERA_ROLL = "camera-roll"
    APP_UI = "app-ui"


class FixtureValidationError(ValueError):
    """A label file does not conform to the fixture schema."""


@dataclass(frozen=True)
class FixtureItem:
    problem_id: str
    prompt_text: str
    student_answer_raw: str
    human_legible: bool
    correct_answer: str


@dataclass(frozen=True)
class FixturePage:
    page_id: str
    image: str
    source_id: str
    subject: str
    capture_quality: str
    capture_device: str
    capture_method: CaptureMethod
    items: tuple[FixtureItem, ...]


def load_fixture_pages(fixtures_dir: Path) -> list[FixturePage]:
    """Load and validate every `*.json` label file directly under `fixtures_dir`."""
    return [_parse_page(path, fixtures_dir) for path in sorted(fixtures_dir.glob("*.json"))]


def _parse_page(label_path: Path, fixtures_dir: Path) -> FixturePage:
    page = _load_object(label_path)

    image = _require_str(page, "image", label_path)
    image_path = fixtures_dir / image
    if not image_path.is_file():
        raise FixtureValidationError(f"{label_path}: image file does not exist: {image_path}")

    capture_method_raw = _require_str(page, "capture_method", label_path)
    try:
        capture_method = CaptureMethod(capture_method_raw)
    except ValueError as exc:
        allowed = ", ".join(m.value for m in CaptureMethod)
        raise FixtureValidationError(
            f"{label_path}: capture_method must be one of {allowed}, "
            f"got {capture_method_raw!r}"
        ) from exc

    return FixturePage(
        page_id=_require_str(page, "page_id", label_path),
        image=image,
        source_id=_require_str(page, "source_id", label_path),
        subject=_require_str(page, "subject", label_path),
        capture_quality=_require_str(page, "capture_quality", label_path),
        capture_device=_normalise_device(_require_str(page, "capture_device", label_path)),
        capture_method=capture_method,
        items=_parse_items(page, label_path),
    )


def _parse_items(page: dict[str, object], label_path: Path) -> tuple[FixtureItem, ...]:
    raw_items = page.get("items")
    if not isinstance(raw_items, list):
        raise FixtureValidationError(f"{label_path}: 'items' must be a list")

    items = tuple(_parse_item(item, label_path) for item in raw_items)
    seen: set[str] = set()
    for item in items:
        if item.problem_id in seen:
            raise FixtureValidationError(f"{label_path}: duplicate problem_id {item.problem_id!r}")
        seen.add(item.problem_id)
    return items


def _parse_item(raw: object, label_path: Path) -> FixtureItem:
    if not isinstance(raw, dict):
        raise FixtureValidationError(f"{label_path}: each item must be an object")
    item: dict[str, object] = raw
    return FixtureItem(
        problem_id=_require_str(item, "problem_id", label_path),
        prompt_text=_require_str(item, "prompt_text", label_path),
        student_answer_raw=_require_str(item, "student_answer_raw", label_path),
        human_legible=_require_bool(item, "human_legible", label_path),
        correct_answer=_require_str(item, "correct_answer", label_path),
    )


def _load_object(label_path: Path) -> dict[str, object]:
    raw: object = json.loads(label_path.read_text())
    if not isinstance(raw, dict):
        raise FixtureValidationError(f"{label_path}: top-level JSON must be an object")
    return raw


def _require_str(page: dict[str, object], key: str, label_path: Path) -> str:
    if key not in page:
        raise FixtureValidationError(f"{label_path}: missing required field '{key}'")
    value = page[key]
    if not isinstance(value, str):
        raise FixtureValidationError(f"{label_path}: '{key}' must be a string, got {value!r}")
    return value


def _require_bool(page: dict[str, object], key: str, label_path: Path) -> bool:
    if key not in page:
        raise FixtureValidationError(f"{label_path}: missing required field '{key}'")
    value = page[key]
    if not isinstance(value, bool):
        raise FixtureValidationError(f"{label_path}: '{key}' must be a boolean, got {value!r}")
    return value


def _normalise_device(value: str) -> str:
    """Fold 'Pixel 9a', 'pixel 9a', and 'pixel-9a' into one slice key."""
    return "-".join(value.strip().lower().split())
