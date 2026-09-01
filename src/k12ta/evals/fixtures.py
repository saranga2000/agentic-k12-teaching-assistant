"""Fixture schema, loader, and promotion path for evaluation pages.

Validates each label file so a malformed fixture fails at load time, not silently
inside a scoring run days later. Does not read image bytes or call a model.

docs/ROADMAP.md's M5 "fixture promotion": every parent correction
(k12ta.store.verdict_correction_audit) is a labelled disagreement between what
the grader said and what a parent said -- exactly the ground truth
docs/EVALS.md family 4 needs, and free where the original M1 corpus was one
deliberate hand-labelling session. `promote_correction` and
`write_correction_fixture` below turn one correction into one more fixture in
this same schema, tagged `provenance="parent-correction"` so it is never
pooled with the hand-labelled corpus in a report (docs/EVALS.md family 1).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class CaptureMethod(Enum):
    """How a labelled page's image reached evals/fixtures/pages/."""

    CAMERA_ROLL = "camera-roll"
    APP_UI = "app-ui"


class Layout(Enum):
    """Whether the image shows one workbook page or two facing pages at once."""

    SINGLE_PAGE = "single-page"
    TWO_PAGE_SPREAD = "two-page-spread"


class SpreadSide(Enum):
    """Which visible page of a two-page spread the labelled items came from."""

    LEFT = "left"
    RIGHT = "right"


class FixtureValidationError(ValueError):
    """A label file does not conform to the fixture schema."""


class FixtureProvenance:
    """docs/EVALS.md family 1: "hand-labelled" for the original M1 corpus,
    "parent-correction" for a page promoted automatically by M5's correction
    loop. A plain string constant set, not an Enum, so an older fixture file
    predating this field (every one of the original M1 corpus) round-trips
    through `_parse_page`'s `page.get(..., HAND_LABELLED)` default without
    needing to be rewritten."""

    HAND_LABELLED = "hand-labelled"
    PARENT_CORRECTION = "parent-correction"


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
    capture_method: CaptureMethod
    layout: Layout
    spread_side: SpreadSide | None
    items: tuple[FixtureItem, ...]
    capture_quality: str | None = None
    """Always given for a hand-labelled fixture (M1's labelling session rates
    it by hand); None for a promoted correction -- a live capture's quality
    has no equivalent live-tracked notion in k12ta.store, and this is never
    guessed at when unknown."""
    capture_device: str | None = None
    """Same reasoning as capture_quality -- a live capture's device is not
    tracked anywhere in k12ta.store today, so a promoted fixture leaves this
    honestly unknown rather than fabricating a value."""
    page_identity: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Ground truth for whatever page-identity markers are legible anywhere in
    this photo (not just the labelled `spread_side`) -- kind -> every distinct
    value actually printed. Empty for a fixture not yet labelled for this; two
    values under one kind is the real, common two-page-spread-with-two-banners
    shape (see docs/ROADMAP.md), never an error."""
    provenance: str = FixtureProvenance.HAND_LABELLED
    """One of FixtureProvenance's constants -- see docs/EVALS.md family 1.
    Defaults to HAND_LABELLED so every fixture written before this field
    existed keeps meaning exactly what it always did."""


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
            f"{label_path}: capture_method must be one of {allowed}, got {capture_method_raw!r}"
        ) from exc

    layout, spread_side = _parse_layout(page, label_path)

    provenance = page.get("provenance", FixtureProvenance.HAND_LABELLED)
    if provenance not in (FixtureProvenance.HAND_LABELLED, FixtureProvenance.PARENT_CORRECTION):
        raise FixtureValidationError(
            f"{label_path}: provenance must be 'hand-labelled' or 'parent-correction', "
            f"got {provenance!r}"
        )

    # capture_quality/capture_device are only ever knowable for a hand-labelled
    # fixture (M1's deliberate labelling session rates them by hand) -- required
    # there, same as always; genuinely unknowable and therefore optional for a
    # promoted correction (FixturePage's own docstrings explain why).
    if provenance == FixtureProvenance.PARENT_CORRECTION:
        capture_quality = _optional_str(page, "capture_quality", label_path)
        capture_device_raw = _optional_str(page, "capture_device", label_path)
    else:
        capture_quality = _require_str(page, "capture_quality", label_path)
        capture_device_raw = _require_str(page, "capture_device", label_path)

    return FixturePage(
        page_id=_require_str(page, "page_id", label_path),
        image=image,
        source_id=_require_str(page, "source_id", label_path),
        subject=_require_str(page, "subject", label_path),
        capture_quality=capture_quality,
        capture_device=(
            normalise_device(capture_device_raw) if capture_device_raw is not None else None
        ),
        capture_method=capture_method,
        layout=layout,
        spread_side=spread_side,
        provenance=str(provenance),
        items=_parse_items(page, label_path),
        page_identity=_parse_page_identity(page, label_path),
    )


def _parse_layout(page: dict[str, object], label_path: Path) -> tuple[Layout, SpreadSide | None]:
    layout_raw = _require_str(page, "layout", label_path)
    try:
        layout = Layout(layout_raw)
    except ValueError as exc:
        allowed = ", ".join(m.value for m in Layout)
        raise FixtureValidationError(
            f"{label_path}: layout must be one of {allowed}, got {layout_raw!r}"
        ) from exc

    if layout is Layout.SINGLE_PAGE:
        if "spread_side" in page:
            raise FixtureValidationError(
                f"{label_path}: spread_side must be absent when layout is single-page"
            )
        return layout, None

    spread_side_raw = _require_str(page, "spread_side", label_path)
    try:
        spread_side = SpreadSide(spread_side_raw)
    except ValueError as exc:
        allowed = ", ".join(m.value for m in SpreadSide)
        raise FixtureValidationError(
            f"{label_path}: spread_side must be one of {allowed}, got {spread_side_raw!r}"
        ) from exc
    return layout, spread_side


def _parse_page_identity(page: dict[str, object], label_path: Path) -> dict[str, tuple[str, ...]]:
    """Component names are open-ended, not drawn from a fixed enum -- a
    composite identity schema is parent-defined per source (Summer Bridge's
    "section"/"day", RSM's "chapter"/"problem_range"), so ground truth must
    accept whatever a real source's schema actually calls its markers. Only the
    shape (a list of non-empty strings per name) is validated."""
    if "page_identity" not in page:
        return {}
    raw = page["page_identity"]
    if not isinstance(raw, dict):
        raise FixtureValidationError(f"{label_path}: 'page_identity' must be an object")
    result: dict[str, tuple[str, ...]] = {}
    for kind, values in raw.items():
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise FixtureValidationError(
                f"{label_path}: page_identity[{kind!r}] must be a list of non-empty strings"
            )
        result[kind] = tuple(values)
    return result


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


def _optional_str(page: dict[str, object], key: str, label_path: Path) -> str | None:
    """Same as _require_str except a missing key is None, not an error --
    capture_quality and capture_device are the only two fields this is used
    for, both unknowable for a promoted correction (see FixturePage's own
    docstrings)."""
    if key not in page:
        return None
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


def normalise_device(value: str) -> str:
    """Fold 'Pixel 9a', 'pixel 9a', and 'pixel-9a' into one slice key.

    Public so the labelling tool can apply the same normalisation before writing a
    label, rather than saving a raw string the loader would silently reshape later.
    """
    return "-".join(value.strip().lower().split())


def build_correction_fixture_item(
    *,
    problem_id: str,
    prompt_text: str,
    student_answer_raw: str,
    expected_answer: str | None,
    new_outcome: str,
) -> FixtureItem | None:
    """The one item a correction promotes, or None when no correct answer is
    actually knowable -- honest about what a correction does and doesn't
    tell you, never a fabricated ground truth:

    - A key's own answer (`expected_answer`), when one is on file, is always
      the ground truth, regardless of `new_outcome` -- a keyed mismatch a
      parent judged still has the key's text to fall back on.
    - Absent a key, a `new_outcome` of "correct" makes the student's own
      (possibly parent-corrected) answer the ground truth -- that is what
      "correct" means.
    - Absent a key, "incorrect" or "partially_correct" names what's wrong,
      not what's right -- there is nothing to promote, so this returns None
      rather than guessing.

    `human_legible` is always True: a parent read this answer directly off
    the physical page to reach this verdict, whether or not the model
    could."""
    if expected_answer is not None:
        correct_answer = expected_answer
    elif new_outcome == "correct":
        correct_answer = student_answer_raw
    else:
        return None
    return FixtureItem(
        problem_id=problem_id,
        prompt_text=prompt_text,
        student_answer_raw=student_answer_raw,
        human_legible=True,
        correct_answer=correct_answer,
    )


def _page_to_json_dict(page: FixturePage) -> dict[str, object]:
    """The inverse of `_parse_page` -- only ever called on a page this module
    itself just built (`write_correction_fixture`), so it assumes a valid
    `FixturePage` rather than re-validating one."""
    data: dict[str, object] = {
        "page_id": page.page_id,
        "image": page.image,
        "source_id": page.source_id,
        "subject": page.subject,
        "capture_method": page.capture_method.value,
        "layout": page.layout.value,
        "items": [
            {
                "problem_id": item.problem_id,
                "prompt_text": item.prompt_text,
                "student_answer_raw": item.student_answer_raw,
                "human_legible": item.human_legible,
                "correct_answer": item.correct_answer,
            }
            for item in page.items
        ],
        "provenance": page.provenance,
    }
    if page.spread_side is not None:
        data["spread_side"] = page.spread_side.value
    if page.capture_quality is not None:
        data["capture_quality"] = page.capture_quality
    if page.capture_device is not None:
        data["capture_device"] = page.capture_device
    if page.page_identity:
        data["page_identity"] = {k: list(v) for k, v in page.page_identity.items()}
    return data


def write_correction_fixture(
    fixtures_dir: Path,
    *,
    page_id: str,
    source_id: str,
    subject: str,
    image_source_path: Path,
    item: FixtureItem,
) -> Path:
    """Copies `image_source_path` (a real page capture already on disk, see
    k12ta.store.captures.PageCaptureRow.image_path) into `fixtures_dir/pages/`
    and writes `fixtures_dir/{page_id}.json`, `provenance="parent-correction"`.
    `capture_quality`/`capture_device` are left None (see FixturePage's own
    docstrings); `capture_method` is always APP_UI, since every live capture
    genuinely came through the app's own capture path, never a camera-roll
    import; `layout` is always SINGLE_PAGE, since the two-tap capture path
    photographs one page at a time (docs/PROMPT_REVIEW.md Gap 3) -- unlike
    M1's corpus, which deliberately includes camera-roll two-page spreads.

    Overwrites cleanly if `page_id` was already promoted once before -- a
    page corrected a second time promotes its current, truer state, not a
    growing pile of near-duplicates of the same page."""
    pages_dir = fixtures_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    image_name = f"{page_id}{image_source_path.suffix}"
    shutil.copyfile(image_source_path, pages_dir / image_name)

    page = FixturePage(
        page_id=page_id,
        image=f"pages/{image_name}",
        source_id=source_id,
        subject=subject,
        capture_method=CaptureMethod.APP_UI,
        layout=Layout.SINGLE_PAGE,
        spread_side=None,
        items=(item,),
        provenance=FixtureProvenance.PARENT_CORRECTION,
    )
    label_path = fixtures_dir / f"{page_id}.json"
    label_path.write_text(json.dumps(_page_to_json_dict(page), indent=2) + "\n")
    return label_path


def promote_correction(
    fixtures_dir: Path,
    *,
    page_id: str,
    source_id: str,
    subject: str,
    image_source_path: Path,
    problem_id: str,
    prompt_text: str,
    student_answer_raw: str,
    expected_answer: str | None,
    new_outcome: str,
) -> Path | None:
    """The one entry point k12ta.keys.app calls after a correction: builds
    the item (build_correction_fixture_item) and, only if a correct answer
    was actually knowable, writes it (write_correction_fixture). Returns the
    path written, or None -- not every correction promotes a fixture, and
    silently skipping is correct, not a failure, when there's nothing honest
    to write (see build_correction_fixture_item)."""
    item = build_correction_fixture_item(
        problem_id=problem_id,
        prompt_text=prompt_text,
        student_answer_raw=student_answer_raw,
        expected_answer=expected_answer,
        new_outcome=new_outcome,
    )
    if item is None:
        return None
    return write_correction_fixture(
        fixtures_dir,
        page_id=page_id,
        source_id=source_id,
        subject=subject,
        image_source_path=image_source_path,
        item=item,
    )
