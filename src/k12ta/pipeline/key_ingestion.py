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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum

from k12ta.config import Settings
from k12ta.ingest.capture import normalize_orientation
from k12ta.store import quota
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

    normalized = normalize_orientation(image_bytes)
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
