"""Vision LLM transcriber for answer-key pages: turns one key photo into structured
(page_number, problem_number, answer) entries.

Never raises, matching the same "never raise, classify and return instead" contract
as `VisionLLMTranscriber`. Deliberately a separate class from it, not a shared
Protocol: a key page's output shape is genuinely different (many page numbers per
photo, an explicit ungradeable reason instead of a student answer), and the input is
raw bytes, never a file path -- key photos are never written to disk.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from k12ta.llm.base import (
    DataRetention,
    MisconfiguredError,
    RateLimitExhaustedError,
    RequestCapExceededError,
    TransientError,
    VisionModel,
)
from k12ta.prompts import load_prompt
from k12ta.transcribe._shared import strip_code_fence
from k12ta.transcribe.base import FailureKind


@dataclass(frozen=True)
class KeyPageEntry:
    page_number: int
    problem_number: str
    answer_text: str | None
    ungradeable_reason: str | None
    """One of "answers_vary" or "graph_or_table" when `answer_text` is None."""
    confidence: float
    """The model's probability that `answer_text` (or the ungradeable
    classification) is exactly correct. Says nothing about the page heading --
    see `identifier_confidence`."""
    identifier_value: str = ""
    """The "Day N" banner text for this entry's block, as printed -- the marker a
    student's own photo will show, distinct from and more legible than the small
    printed page number (docs/ROADMAP.md's page-identity discussion). Empty string
    if the model didn't report one, never a guess."""
    identifier_confidence: float = 0.0
    """The model's probability that `identifier_value` and `page_number` are
    exactly correct, independent of `confidence` -- a block heading can be
    smudged even when the answer next to it reads perfectly clearly. This is
    what gates the confirm screen's "unconfirmed" marking on manual identifier
    entry (k12ta.keys.app), never `confidence`."""


@dataclass(frozen=True)
class KeyPageResult:
    entries: tuple[KeyPageEntry, ...]
    provider: str
    model: str
    cost_usd: float
    latency_ms: int
    data_retention: DataRetention
    failure: str | None = None
    failure_kind: FailureKind | None = None


class KeyTranscriber(Protocol):
    """Anything that turns one answer-key page photo into structured entries."""

    name: str

    @property
    def request_count(self) -> int: ...

    def transcribe(
        self, image_bytes: bytes, on_progress: Callable[[int], None] | None = None
    ) -> KeyPageResult:
        """Read one key page. Must not raise; classify any failure and return it.
        `on_progress`, if given, is called with the cumulative character count
        received so far -- passed straight through to the underlying VisionModel,
        see its docstring."""
        ...


_FAILURE_KIND_BY_ERROR: dict[type[Exception], FailureKind] = {
    MisconfiguredError: FailureKind.MISCONFIGURED,
    RateLimitExhaustedError: FailureKind.RATE_LIMITED,
    RequestCapExceededError: FailureKind.REQUEST_CAP_EXCEEDED,
    TransientError: FailureKind.TRANSIENT,
}


def _classify(exc: Exception) -> FailureKind:
    for error_type, kind in _FAILURE_KIND_BY_ERROR.items():
        if isinstance(exc, error_type):
            return kind
    return FailureKind.UNREADABLE


class VisionLLMKeyTranscriber:
    """Reads one answer-key page photo via a multimodal model."""

    name = "vision_llm_key"

    def __init__(
        self,
        vision_model: VisionModel,
        provider: str,
        model: str,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._vision_model = vision_model
        self._provider = provider
        self._model = model
        self._prompt = load_prompt("transcribe_key_page")
        self._monotonic = monotonic

    @property
    def request_count(self) -> int:
        return self._vision_model.request_count

    def transcribe(
        self, image_bytes: bytes, on_progress: Callable[[int], None] | None = None
    ) -> KeyPageResult:
        started = self._monotonic()
        try:
            response = self._vision_model.generate(
                self._prompt, image_bytes, "image/jpeg", on_progress=on_progress
            )
            entries = _parse_entries(response.text)
            return KeyPageResult(
                entries=entries,
                provider=self._provider,
                model=self._model,
                cost_usd=float(response.cost_usd),
                latency_ms=response.latency_ms,
                data_retention=self._vision_model.data_retention,
            )
        except Exception as exc:
            # Timed here, not taken from `response`: a failure (a 5xx, a retry
            # exhausted after real backoff sleeps) never produces a VisionResponse
            # to read a latency off of, but it still cost real wall-clock time --
            # the one number that tells a log whether a stuck request was slow or
            # truly stuck. Hardcoding this to 0 would throw that evidence away.
            elapsed_ms = int((self._monotonic() - started) * 1000)
            return KeyPageResult(
                entries=(),
                provider=self._provider,
                model=self._model,
                cost_usd=0.0,
                latency_ms=elapsed_ms,
                data_retention=self._vision_model.data_retention,
                failure=f"{type(exc).__name__}: {exc}",
                failure_kind=_classify(exc),
            )


def _parse_entries(text: str) -> tuple[KeyPageEntry, ...]:
    payload = json.loads(strip_code_fence(text))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("response JSON has no 'entries' list")
    return tuple(_parse_entry(entry) for entry in raw_entries if isinstance(entry, dict))


def _parse_entry(raw: dict[str, object]) -> KeyPageEntry:
    confidence = raw.get("confidence")
    valid_confidence = isinstance(confidence, int | float) and not isinstance(confidence, bool)
    identifier_confidence = raw.get("identifier_confidence")
    valid_identifier_confidence = isinstance(identifier_confidence, int | float) and not isinstance(
        identifier_confidence, bool
    )
    page_number = raw.get("page_number")
    answer_text = raw.get("answer_text")
    ungradeable_reason = raw.get("ungradeable_reason")
    identifier_value = raw.get("identifier_value")
    return KeyPageEntry(
        page_number=int(page_number) if isinstance(page_number, int | float) else 0,
        problem_number=str(raw.get("problem_number", "")),
        answer_text=answer_text if isinstance(answer_text, str) else None,
        ungradeable_reason=ungradeable_reason if isinstance(ungradeable_reason, str) else None,
        confidence=float(confidence) if valid_confidence else 0.0,  # type: ignore[arg-type]
        identifier_value=identifier_value if isinstance(identifier_value, str) else "",
        identifier_confidence=(
            float(identifier_confidence)  # type: ignore[arg-type]
            if valid_identifier_confidence
            else 0.0
        ),
    )
