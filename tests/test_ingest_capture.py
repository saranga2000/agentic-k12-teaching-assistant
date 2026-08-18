"""The M2.2 quality gate and default-assignment resolution, no HTTP involved."""

from __future__ import annotations

import io
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from PIL import Image

from k12ta.config import Settings
from k12ta.ingest import capture, schedule
from k12ta.store import captures, content, db, migrate, students
from k12ta.store import schedule as store_schedule


def _jpeg_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


_EXIF_ORIENTATION_TAG = 0x0112


def _jpeg_bytes_with_exif_orientation(
    size: tuple[int, int], color: tuple[int, int, int], orientation: int
) -> bytes:
    """A JPEG whose raw pixel buffer is `size`, tagged with an EXIF orientation --
    exactly how a phone/tablet camera stores a photo: the sensor buffer is not
    physically rotated, a metadata tag says how to display it upright instead."""
    image = Image.new("RGB", size, color=color)
    exif = image.getexif()
    exif[_EXIF_ORIENTATION_TAG] = orientation
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


TOO_SMALL = _jpeg_bytes((10, 10), (255, 255, 255))
TOO_DARK = _jpeg_bytes((1200, 1600), (5, 5, 5))
LOOKS_LIKE_TWO_PAGES = _jpeg_bytes((1600, 1200), (200, 200, 200))
ACCEPTED = _jpeg_bytes((1200, 1600), (200, 200, 200))

# A single page held in portrait, as an iPad camera actually stores it: raw buffer
# 1600x1200 (landscape), EXIF orientation 6 ("rotate 90 CW to display upright"). Every
# consumer that ignores the tag sees a 1600x1200 image and misreads it as a spread.
PORTRAIT_STORED_SIDEWAYS = _jpeg_bytes_with_exif_orientation((1600, 1200), (210, 210, 210), 6)


def _heic_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    """A real HEIC file, not a renamed JPEG -- built with pillow-heif's own writer
    so this test proves the actual format iPhone/iPad cameras produce decodes,
    not just a file with a .heic-shaped name."""
    import pillow_heif

    buf = io.BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", size, color=color)).save(buf, quality=90)
    return buf.getvalue()


A_HEIC_PHOTO = _heic_bytes((1200, 1600), (200, 200, 200))


def test_rejects_an_image_that_is_too_small() -> None:
    verdict = capture.evaluate_image_quality(TOO_SMALL)
    assert verdict.accepted is False
    assert verdict.reason == "too_small"


def test_rejects_an_image_that_is_too_dark() -> None:
    verdict = capture.evaluate_image_quality(TOO_DARK)
    assert verdict.accepted is False
    assert verdict.reason == "too_dark"


def test_rejects_a_landscape_image_as_a_likely_two_page_spread() -> None:
    verdict = capture.evaluate_image_quality(LOOKS_LIKE_TWO_PAGES)
    assert verdict.accepted is False
    assert verdict.reason == "looks_like_two_pages"


def test_accepts_a_large_bright_portrait_image() -> None:
    verdict = capture.evaluate_image_quality(ACCEPTED)
    assert verdict.accepted is True
    assert verdict.reason is None


def test_normalize_orientation_corrects_a_sideways_stored_portrait_photo() -> None:
    normalized = capture.normalize_orientation(PORTRAIT_STORED_SIDEWAYS)

    assert Image.open(io.BytesIO(normalized)).size == (1200, 1600)


def test_normalize_orientation_leaves_an_already_upright_photo_unchanged_in_size() -> None:
    normalized = capture.normalize_orientation(ACCEPTED)

    assert Image.open(io.BytesIO(normalized)).size == (1200, 1600)


def test_a_sideways_stored_single_page_photo_is_accepted_once_normalized() -> None:
    """The exact bug reported live: a portrait single-page photo, stored the way a
    real camera stores it, was rejected as a two-page spread every time because the
    quality gate read the raw (landscape) buffer dimensions instead of the corrected
    ones. This is the regression test for that."""
    normalized = capture.normalize_orientation(PORTRAIT_STORED_SIDEWAYS)

    verdict = capture.evaluate_image_quality(normalized)

    assert verdict.accepted is True
    assert verdict.reason is None


def test_normalize_orientation_decodes_a_heic_photo() -> None:
    """The live bug this exists for: the default format every iPhone and iPad
    camera produces (the household's own devices) crashed normalize_orientation
    outright before pillow-heif was registered -- Pillow alone cannot open HEIC.
    The Pixel one child used shoots JPEG, which is the only reason this went
    unnoticed until now."""
    normalized = capture.normalize_orientation(A_HEIC_PHOTO)

    reopened = Image.open(io.BytesIO(normalized))
    assert reopened.format == "JPEG"
    assert reopened.size == (1200, 1600)


def test_a_heic_photo_is_accepted_once_normalized() -> None:
    normalized = capture.normalize_orientation(A_HEIC_PHOTO)

    verdict = capture.evaluate_image_quality(normalized)

    assert verdict.accepted is True
    assert verdict.reason is None


def test_unnormalized_sideways_photo_still_misreads_as_a_spread() -> None:
    """Documents why normalization has to run first: without it, the exact same
    photo is misclassified. If this test ever starts failing, evaluate_image_quality
    has started reading orientation correctly on its own and this test (not the
    normalization step) is what's now redundant."""
    verdict = capture.evaluate_image_quality(PORTRAIT_STORED_SIDEWAYS)

    assert verdict.accepted is False
    assert verdict.reason == "looks_like_two_pages"


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_student_with_source(conn: sqlite3.Connection, student_id: str) -> None:
    students.insert_student(
        conn,
        students.StudentRow(
            student_id=student_id,
            display_name="Marcus",
            grade_level=7,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id=student_id,
            source_id="summer_bridge",
            label="Summer bridge workbook",
            kind="workbook",
            subject="math",
            has_answer_key=True,
            graded_by_someone_else=False,
            default_mode="full",
            typical_session_minutes=30,
        ),
    )


def test_resolve_default_source_returns_none_when_nothing_is_scheduled() -> None:
    conn = _migrated_connection()
    _seed_student_with_source(conn, "s-marcus")
    on = date(2026, 8, 12)

    assert schedule.resolve_default_source(conn, "s-marcus", on) is None


def test_resolve_default_source_returns_the_scheduled_content_source() -> None:
    conn = _migrated_connection()
    _seed_student_with_source(conn, "s-marcus")
    on = date(2026, 8, 12)
    store_schedule.set_default_source(
        conn,
        store_schedule.WeeklyDefaultSourceRow(
            student_id="s-marcus", weekday=on.weekday(), source_id="summer_bridge"
        ),
    )

    resolved = schedule.resolve_default_source(conn, "s-marcus", on)

    assert resolved is not None
    assert resolved.source_id == "summer_bridge"


def test_get_or_create_todays_assignment_is_idempotent_for_the_same_day() -> None:
    conn = _migrated_connection()
    _seed_student_with_source(conn, "s-marcus")
    on = date(2026, 8, 12)

    first = schedule.get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", on)
    second = schedule.get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", on)

    assert first.assignment_id == second.assignment_id
    cur = conn.execute(
        "SELECT COUNT(*) FROM assignments WHERE student_id = ? AND source_id = ?",
        ("s-marcus", "summer_bridge"),
    )
    assert cur.fetchone()[0] == 1


def test_save_capture_writes_the_image_and_a_page_captures_row(tmp_path: Path) -> None:
    conn = _migrated_connection()
    _seed_student_with_source(conn, "s-marcus")
    on = date(2026, 8, 12)
    assignment = schedule.get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", on)
    settings = Settings(
        llm_provider="anthropic",
        llm_api_key="",
        llm_model="",
        llm_max_requests_per_run=40,
        data_dir=tmp_path,
        coach_name="Coach",
        daily_token_budget_usd=Decimal("1.50"),
        daily_request_limit=20,
        log_level="INFO",
    )

    row = capture.save_capture(conn, settings, "s-marcus", assignment.assignment_id, ACCEPTED)

    assert Path(row.image_path).exists()
    assert Path(row.image_path).read_bytes() == ACCEPTED

    fetched = captures.get_page_capture(conn, "s-marcus", row.capture_id)
    assert fetched is not None
    assert fetched.assignment_id == assignment.assignment_id
