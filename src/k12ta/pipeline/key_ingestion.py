"""Orchestrating one answer-key page: quota gate -> normalize -> transcribe.

Sibling to `process.py`, not merged into it: different persistence rules. This
module never writes to `answer_key_entries` -- nothing enters the key store until a
parent confirms it, which happens one layer up, in `k12ta.keys`. Reuses the exact
same daily quota gate as `process_capture` (same `k12ta.store.quota` table, checked
first, before anything else happens) and the same orientation fix
(`k12ta.ingest.capture.normalize_orientation`) -- key photos come from the same kind
of camera and hit the same EXIF issue student photos did.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from uuid import uuid4

from k12ta.config import Settings
from k12ta.ingest.capture import normalize_orientation
from k12ta.store import quota
from k12ta.transcribe.base import Transcriber
from k12ta.transcribe.key_page import KeyPageEntry, KeyTranscriber

logger = logging.getLogger(__name__)


class KeyIngestionStatus(Enum):
    QUOTA_EXHAUSTED = "quota_exhausted"
    TRANSCRIBE_FAILED = "transcribe_failed"
    TRANSCRIBED = "transcribed"


@dataclass(frozen=True)
class KeyIngestionOutcome:
    status: KeyIngestionStatus
    entries: tuple[KeyPageEntry, ...] = ()
    normalized_image_bytes: bytes | None = None
    """Set on TRANSCRIBED: the confirm screen shows this next to the extracted
    entries so a parent can actually verify them, not confirm blind."""
    failure_reason: str | None = None

    @staticmethod
    def quota_exhausted() -> KeyIngestionOutcome:
        return KeyIngestionOutcome(status=KeyIngestionStatus.QUOTA_EXHAUSTED)

    @staticmethod
    def transcribe_failed(reason: str) -> KeyIngestionOutcome:
        return KeyIngestionOutcome(
            status=KeyIngestionStatus.TRANSCRIBE_FAILED, failure_reason=reason
        )

    @staticmethod
    def transcribed(
        entries: tuple[KeyPageEntry, ...], normalized_image_bytes: bytes
    ) -> KeyIngestionOutcome:
        return KeyIngestionOutcome(
            status=KeyIngestionStatus.TRANSCRIBED,
            entries=entries,
            normalized_image_bytes=normalized_image_bytes,
        )


def save_key_page_image(settings: Settings, image_bytes: bytes) -> str:
    """Write a transcribed key scan to disk and return its path -- called once a
    scan has reached the confirm screen (there is something worth keeping a photo
    of), not at raw upload, so a failed or quota-blocked call never litters
    `key_captures/` with an image nothing will ever reference. No DB row here:
    the file exists independent of whether a parent goes on to confirm anything
    from it; `k12ta.keys.app.submit_confirm` is what links specific page numbers
    to this path, in `k12ta.store.key_page_images`, once it knows which pages
    were actually saved. Mirrors `k12ta.ingest.capture.save_capture`'s shape,
    not shared with it: that function also writes a `page_captures` row, which
    has no equivalent here -- a key scan's row-level persistence is
    `answer_key_entries`/`key_page_images`, both written later, by the parent's
    own confirm."""
    destination = settings.data_dir / "key_captures" / f"{uuid4()}.jpg"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(image_bytes)
    return str(destination)


def transcribe_key_page(
    conn: sqlite3.Connection,
    settings: Settings,
    get_transcriber: Callable[[], KeyTranscriber],
    image_bytes: bytes,
    on_progress: Callable[[int], None] | None = None,
    identity_schema: Sequence[tuple[str, str | None]] = (),
) -> KeyIngestionOutcome:
    """Quota-gated, one call to `get_transcriber()().transcribe`, no retry loop.

    `get_transcriber` is a factory for the same reason `process_capture`'s is: a
    quota-exhausted key photo must never pay the cost of building a live vision-model
    adapter, and a broken provider config must never 500 a request that was never
    going to reach the model. `on_progress`, if given, is passed straight through to
    the transcriber -- see `k12ta.llm.base.VisionModel.generate`'s docstring.
    `identity_schema` is the source's current identity components, as
    `(component_name, example)` pairs in schema position order -- this module has
    no student/source context of its own (that lives one layer up, in
    `k12ta.keys`, which already loaded `source` to get here), so the caller loads
    the schema and passes it straight through. Empty means discovery mode: no
    schema exists yet for this source, or this is deliberately its first scan.
    """
    today = date.today()
    if quota.get_count(conn, today) >= settings.daily_request_limit:
        logger.info("key page blocked: daily quota exhausted")
        return KeyIngestionOutcome.quota_exhausted()

    try:
        normalized = normalize_orientation(image_bytes)
    except Exception as exc:
        # Must not raise: this runs inside k12ta.keys.app's background worker
        # thread, which has no exception handling of its own -- an escaped
        # exception here doesn't crash cleanly, it hangs the request forever
        # (the worker thread dies silently, the main thread's queue.get() never
        # returns). Caught before quota.record_request on purpose: a file the
        # model was never even sent must not spend the day's quota.
        reason = f"{type(exc).__name__}: {exc}"
        logger.info("key page normalize outcome=failed reason=%s", reason)
        return KeyIngestionOutcome.transcribe_failed(reason)

    quota.record_request(conn, today)

    try:
        transcriber = get_transcriber()
        result = transcriber.transcribe(
            normalized, on_progress=on_progress, identity_schema=identity_schema
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.info("key page transcribe outcome=failed reason=%s", reason)
        return KeyIngestionOutcome.transcribe_failed(reason)

    logger.info(
        "key page transcribe outcome=%s cost_usd=%s latency_ms=%s",
        "failed" if result.failure is not None else "ok",
        result.cost_usd,
        result.latency_ms,
    )

    if result.failure is not None:
        return KeyIngestionOutcome.transcribe_failed(result.failure)

    return KeyIngestionOutcome.transcribed(result.entries, normalized)


def discover_identity_from_example_page(
    conn: sqlite3.Connection,
    settings: Settings,
    get_transcriber: Callable[[], Transcriber],
    image_bytes: bytes,
) -> Mapping[str, str]:
    """Gap I (docs/USER_WORKFLOWS.md): a parent's optional second photo
    alongside a key scan -- an ordinary exercise page, no answers needed --
    for identity markers that show there but not on the isolated key page. A
    real gap the RSM material demonstrated: some answer-key editions print no
    chapter/lesson banner at all, while the matching exercise page does.

    Reuses the same student-side `Transcriber` discovery mode
    `k12ta.pipeline.process.process_capture` already relies on
    (`identity_schema=()` means "report whatever markers are there"), never a
    second, easier-to-drift-from extraction path. Quota-gated exactly like
    the key page's own call, in the same order (quota check, then normalize,
    then record, then transcribe) -- this is a second real request, not a
    free extra.

    Best-effort only: any failure (quota exhausted, an unreadable photo, a
    rate limit, a transcribe error) returns an empty mapping rather than
    raising -- this rides along with a key upload that must still succeed or
    fail entirely on its own terms, regardless of what this bonus call finds.
    The image is never persisted: unlike a key scan or a student capture,
    nothing downstream ever needs to look at it again once this call returns.
    """
    today = date.today()
    if quota.get_count(conn, today) >= settings.daily_request_limit:
        logger.info("example-page discovery skipped: daily quota exhausted")
        return {}

    try:
        normalized = normalize_orientation(image_bytes)
    except Exception as exc:
        logger.info("example-page discovery normalize failed: %s: %s", type(exc).__name__, exc)
        return {}

    quota.record_request(conn, today)

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
        handle.write(normalized)
        tmp_path = Path(handle.name)
    try:
        result = get_transcriber().transcribe(str(tmp_path))
    except Exception as exc:
        logger.info("example-page discovery transcribe failed: %s: %s", type(exc).__name__, exc)
        return {}
    finally:
        tmp_path.unlink(missing_ok=True)

    if result.failure is not None:
        logger.info("example-page discovery transcribe outcome=failed reason=%s", result.failure)
        return {}

    return {
        name: values[0] for name, values in result.page_identity.candidates.items() if values
    }
