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
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pillow_heif
from PIL import Image, ImageOps, ImageStat

from k12ta.config import Settings
from k12ta.store import captures

logger = logging.getLogger(__name__)

pillow_heif.register_heif_opener()
"""Module-level, run once at import time: makes Image.open transparently decode
HEIC, the default format every iPhone and iPad camera produces, the same way it
already handles JPEG and PNG. Without this, normalize_orientation's Image.open
call below raises UnidentifiedImageError on a real HEIC upload -- not a
screenshot-era edge case, a crash on the household's own primary devices."""

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


def normalize_orientation(image_bytes: bytes) -> bytes:
    """Physically rotate pixels to match the photo's EXIF orientation, then
    re-encode without it.

    Phone and tablet cameras record orientation as metadata rather than rotating
    the sensor's buffer, so a portrait photo is commonly stored with a landscape
    width/height and an EXIF tag saying how to display it upright. Every consumer
    downstream -- the reject gate, the saved file, whatever eventually reads the
    file -- needs to agree on which way is up, so this runs once, first, before
    `evaluate_image_quality` or `save_capture` ever sees the bytes. Re-encoding
    without carrying the EXIF block forward is deliberate, not an oversight: it
    also strips whatever else a phone embeds by default, GPS location included,
    from a photo of a child's homework.
    """
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes)))
    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()


def _reject(reason: str, width: int, height: int) -> QualityVerdict:
    """A rejected photo is never saved to disk (save_capture only runs after
    acceptance), so this log line is the only record of what a real rejection
    actually looked like -- the gap a live incident exposed: a parent's
    paraphrase of the on-screen message was the only evidence available, and
    the real cause had to be guessed rather than read off a log."""
    ratio = width / height if height else None
    logger.info(
        "capture rejected reason=%s width=%d height=%d ratio=%s",
        reason,
        width,
        height,
        f"{ratio:.3f}" if ratio is not None else "n/a",
    )
    return QualityVerdict(accepted=False, reason=reason)


def evaluate_image_quality(image_bytes: bytes, *, check_for_spread: bool = True) -> QualityVerdict:
    """`check_for_spread=False` for a source configured as
    SourceKind.ONLINE_EXERCISE: the aspect-ratio heuristic below assumes a
    photograph of a physical page (portrait) versus two pages photographed flat
    (landscape) -- an assumption a screenshot's own shape has no relation to.
    Configuration, decided by the caller from the assignment's source kind,
    never guessed here from the image itself."""
    image = Image.open(io.BytesIO(image_bytes))
    width, height = image.size

    if width < MIN_DIMENSION_PX or height < MIN_DIMENSION_PX:
        return _reject("too_small", width, height)

    brightness = ImageStat.Stat(image.convert("L")).mean[0]
    if brightness < DARK_MEAN_BRIGHTNESS_THRESHOLD:
        return _reject("too_dark", width, height)

    if check_for_spread and width / height >= SPREAD_ASPECT_RATIO_THRESHOLD:
        return _reject("looks_like_two_pages", width, height)

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
