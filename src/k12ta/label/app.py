"""Local tool for hand-labelling fixture pages. A tool for the parent, not a product
surface: no auth, no multi-user concerns, run only on the household machine.

    python -m k12ta.label

Progress is resumable by construction: each page is written to its own JSON file the
moment it is saved, so "next unlabelled" is just "the first image with no matching
file." Killing the server loses nothing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from k12ta.evals.fixtures import CaptureMethod, Layout, SpreadSide, normalise_device

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "evals" / "fixtures"
PAGES_DIR = FIXTURES_DIR / "pages"
CACHE_DIR = FIXTURES_DIR / ".cache"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic"}
DEFAULT_ROWS = 10
ADD_ROWS = 5
PAGE_FIELDS = (
    "source_id",
    "subject",
    "capture_quality",
    "capture_device",
    "capture_method",
    "layout",
    "spread_side",
)
# layout and spread_side are page-specific judgements, not carried between pages.
PREFILL_FIELDS = ("source_id", "subject", "capture_method")
ITEM_TEXT_FIELDS = ("problem_id", "prompt_text", "student_answer_raw", "correct_answer")

app = FastAPI()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _all_images() -> list[Path]:
    return sorted(p for p in PAGES_DIR.iterdir() if p.suffix.lower() in IMAGE_EXTS)


def _stem(image: Path) -> str:
    return image.stem.lower()


def _labelled_stems() -> set[str]:
    return {p.stem for p in FIXTURES_DIR.glob("*.json")}


def _next_unlabelled() -> Path | None:
    done = _labelled_stems()
    for image in _all_images():
        if _stem(image) not in done:
            return image
    return None


def _label_path(stem: str) -> Path:
    return FIXTURES_DIR / f"{stem}.json"


def _load_json(path: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads(path.read_text())
    return data


def _last_saved() -> dict[str, object] | None:
    files = sorted(FIXTURES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    return _load_json(files[-1]) if files else None


def _row_from_saved(item: object) -> dict[str, str]:
    if not isinstance(item, dict):
        return _blank_row()
    return {
        "problem_id": str(item.get("problem_id", "")),
        "prompt_text": str(item.get("prompt_text", "")),
        "student_answer_raw": str(item.get("student_answer_raw", "")),
        "correct_answer": str(item.get("correct_answer", "")),
        "human_legible": "1" if item.get("human_legible") else "",
    }


def _form_defaults(stem: str) -> tuple[dict[str, str], list[dict[str, str]], int, set[str]]:
    """Values to pre-fill for `stem`: its own saved record if one exists (editing),
    otherwise a partial carry-forward from the most recently saved page (fresh)."""
    existing = _label_path(stem)
    if existing.exists():
        saved = _load_json(existing)
        values = {field: str(saved.get(field) or "") for field in PAGE_FIELDS}
        raw_items = saved.get("items")
        rows = [_row_from_saved(item) for item in raw_items] if isinstance(raw_items, list) else []
        while len(rows) < DEFAULT_ROWS:
            rows.append(_blank_row())
        return values, rows, len(rows), set()

    last = _last_saved() or {}
    values = {field: "" for field in PAGE_FIELDS}
    prefilled: set[str] = set()
    for field in PREFILL_FIELDS:
        value = last.get(field)
        if isinstance(value, str) and value:
            values[field] = value
            prefilled.add(field)
    rows = [_blank_row() for _ in range(DEFAULT_ROWS)]
    return values, rows, DEFAULT_ROWS, prefilled


def _display_copy(image: Path) -> Path:
    """Return a browser-displayable copy of `image`, converting and caching if HEIC."""
    if image.suffix.lower() != ".heic":
        return image
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"{image.stem}.jpg"
    if not cached.exists() or cached.stat().st_mtime < image.stat().st_mtime:
        subprocess.run(
            ["sips", "-s", "format", "jpeg", str(image), "--out", str(cached)],
            check=True,
            capture_output=True,
        )
    return cached


def _blank_row() -> dict[str, str]:
    row = {field: "" for field in ITEM_TEXT_FIELDS}
    row["human_legible"] = ""
    return row


def _get(data: dict[str, list[str]], key: str, default: str = "") -> str:
    return data.get(key, [default])[0]


def _validate_layout(layout_raw: str, spread_side_raw: str) -> str | None:
    if not layout_raw:
        return "layout is required."
    if layout_raw not in {m.value for m in Layout}:
        return f"layout must be one of {', '.join(m.value for m in Layout)}."
    if layout_raw == Layout.TWO_PAGE_SPREAD.value and spread_side_raw not in {
        s.value for s in SpreadSide
    }:
        return "spread_side is required when layout is two-page-spread."
    return None


def _render_form(
    request: Request,
    image: Path,
    values: dict[str, str],
    rows: list[dict[str, str]],
    row_count: int,
    prefilled: set[str],
    error: str | None = None,
) -> HTMLResponse:
    context = {
        "stem": _stem(image),
        "filename": image.name,
        "done": len(_labelled_stems()),
        "total": len(_all_images()),
        "values": values,
        "rows": rows,
        "row_count": row_count,
        "prefilled": prefilled,
        "methods": [m.value for m in CaptureMethod],
        "layouts": [layout.value for layout in Layout],
        "spread_sides": [side.value for side in SpreadSide],
        "error": error,
    }
    return templates.TemplateResponse(request, "label.html", context)


@app.get("/", response_class=HTMLResponse)
def index() -> RedirectResponse:
    return RedirectResponse("/label")


@app.get("/image/{stem}")
def serve_image(stem: str) -> FileResponse:
    for image in _all_images():
        if _stem(image) == stem:
            return FileResponse(_display_copy(image))
    raise HTTPException(404, "no such image")


@app.get("/pages", response_class=HTMLResponse)
def list_pages(request: Request) -> HTMLResponse:
    done = _labelled_stems()
    rows = [
        {"stem": _stem(image), "filename": image.name, "done": _stem(image) in done}
        for image in _all_images()
    ]
    context = {"rows": rows, "done": len(done), "total": len(rows)}
    return templates.TemplateResponse(request, "pages.html", context)


@app.get("/label", response_model=None)
def label_next(request: Request) -> HTMLResponse | RedirectResponse:
    image = _next_unlabelled()
    if image is None:
        return templates.TemplateResponse(request, "done.html", {"total": len(_all_images())})
    return RedirectResponse(f"/label/{_stem(image)}")


@app.get("/label/{stem}", response_class=HTMLResponse)
def label_specific(request: Request, stem: str) -> HTMLResponse:
    image = next((p for p in _all_images() if _stem(p) == stem), None)
    if image is None:
        raise HTTPException(404, "no such image")
    values, rows, row_count, prefilled = _form_defaults(stem)
    return _render_form(request, image, values, rows, row_count, prefilled)


@app.post("/label", response_model=None)
async def submit_label(request: Request) -> HTMLResponse | RedirectResponse:
    data = parse_qs((await request.body()).decode())
    stem = _get(data, "stem")
    image = next((p for p in _all_images() if _stem(p) == stem), None)
    if image is None:
        raise HTTPException(404, "no such image")

    was_already_labelled = _label_path(stem).exists()
    action = _get(data, "action", "save")
    row_count = int(_get(data, "row_count", str(DEFAULT_ROWS)))
    values = {field: _get(data, field) for field in PAGE_FIELDS}
    rows = [
        {
            "problem_id": _get(data, f"problem_id_{i}"),
            "prompt_text": _get(data, f"prompt_text_{i}"),
            "student_answer_raw": _get(data, f"student_answer_raw_{i}"),
            "correct_answer": _get(data, f"correct_answer_{i}"),
            "human_legible": _get(data, f"human_legible_{i}"),
        }
        for i in range(row_count)
    ]

    if action == "add_rows":
        new_rows = rows + [_blank_row() for _ in range(ADD_ROWS)]
        return _render_form(request, image, values, new_rows, row_count + ADD_ROWS, set())

    layout_raw = values["layout"].strip()
    spread_side_raw = values["spread_side"].strip()
    layout_error = _validate_layout(layout_raw, spread_side_raw)
    if layout_error:
        return _render_form(request, image, values, rows, row_count, set(), error=layout_error)

    items = [
        {
            "problem_id": row["problem_id"].strip(),
            "prompt_text": row["prompt_text"].strip(),
            "student_answer_raw": row["student_answer_raw"].strip(),
            "human_legible": bool(row["human_legible"]),
            "correct_answer": row["correct_answer"].strip(),
        }
        for row in rows
        if row["problem_id"].strip()
    ]

    if action == "save" and not items:
        error = "No problems entered. Use 'Skip this page' if it has none."
        return _render_form(request, image, values, rows, row_count, set(), error=error)

    page: dict[str, object] = {
        "page_id": stem,
        "image": f"pages/{image.name}",
        "source_id": values["source_id"].strip(),
        "subject": values["subject"].strip(),
        "capture_quality": values["capture_quality"].strip(),
        "capture_device": normalise_device(values["capture_device"]),
        "capture_method": values["capture_method"],
        "layout": layout_raw,
        "items": items,
    }
    if layout_raw == Layout.TWO_PAGE_SPREAD.value:
        page["spread_side"] = spread_side_raw
    _label_path(stem).write_text(json.dumps(page, indent=2))
    destination = "/pages" if was_already_labelled else "/label"
    return RedirectResponse(destination, status_code=303)
