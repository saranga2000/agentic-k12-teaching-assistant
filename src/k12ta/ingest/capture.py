"""The capture-quality reject gate and writing an accepted photo to disk.

Guiding a student to photograph one legible page is worth more than any prompt tuning
available downstream: the 2026-08-12 transcription eval measured 0.396 detection
recall on a corpus that was 100% two-page spreads with zero misnumbered items, meaning
every miss was a clean failure to detect the problem at all, not a numbering mixup.
This gate rejects the photo before it ever reaches a model.

The two-page-spread check is an aspect-ratio heuristic, not real skew or perspective
detection (that needs OpenCV, out of scope here): a single page held up and
photographed is portrait, a two-page spread photographed flat is landscape.
"""

from __future__ import annotations

import io
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from PIL import Image, ImageStat

from k12ta.config import Settings
from k12ta.store import captures

MIN_DIMENSION_PX = 600
DARK_MEAN_BRIGHTNESS_THRESHOLD = 50.0
"""Grayscale mean, 0-255. Below this the page is unlikely to be legible."""
SPREAD_ASPECT_RATIO_THRESHOLD = 1.05
"""width / height at or above this reads as landscape: a spread, not a single page."""


@dataclass(frozen=True)
class QualityVerdict:
    accepted: bool
    reason: str | None
    """One of "too_small", "too_dark", "looks_like_two_pages" when not accepted."""


def evaluate_image_quality(image_bytes: bytes) -> QualityVerdict:
    image = Image.open(io.BytesIO(image_bytes))
    width, height = image.size

    if width < MIN_DIMENSION_PX or height < MIN_DIMENSION_PX:
        return QualityVerdict(accepted=False, reason="too_small")

    brightness = ImageStat.Stat(image.convert("L")).mean[0]
    if brightness < DARK_MEAN_BRIGHTNESS_THRESHOLD:
        return QualityVerdict(accepted=False, reason="too_dark")

    if width / height >= SPREAD_ASPECT_RATIO_THRESHOLD:
        return QualityVerdict(accepted=False, reason="looks_like_two_pages")

    return QualityVerdict(accepted=True, reason=None)


def save_capture(
    conn: sqlite3.Connection,
    settings: Settings,
    student_id: str,
    assignment_id: str,
    image_bytes: bytes,
) -> captures.PageCaptureRow:
    """Write an already-accepted photo to disk and record its page_captures row.

    Callers must run `evaluate_image_quality` first; this function does not re-check.
    """
    capture_id = str(uuid4())
    destination = settings.data_dir / "captures" / f"{capture_id}.jpg"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(image_bytes)

    row = captures.PageCaptureRow(
        student_id=student_id,
        capture_id=capture_id,
        assignment_id=assignment_id,
        captured_at=datetime.now(UTC).isoformat(),
        image_path=str(destination),
    )
    captures.insert_page_capture(conn, row)
    return row
