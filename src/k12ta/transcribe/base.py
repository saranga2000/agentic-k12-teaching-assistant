"""Transcriber provider abstraction.

Phase 1 has exactly one implementation (a vision LLM). The abstraction exists now so
that adding a second reader later is a new file, not a rewrite, and so that the eval
harness can score any provider through one interface.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from k12ta.llm.base import DataRetention


class FailureKind(Enum):
    """Why a page transcription failed, so a caller can tell "stop the run" apart from
    "this one page didn't work."

    MISCONFIGURED and RATE_LIMITED (and REQUEST_CAP_EXCEEDED) mean every remaining page
    would fail the same way for a reason that has nothing to do with that page's
    content — the run should abort rather than repeat the same loss on each one.
    TRANSIENT and UNREADABLE are properties of this one call or this one page; the run
    should record the failure and continue.
    """

    MISCONFIGURED = "misconfigured"
    RATE_LIMITED = "rate_limited"
    REQUEST_CAP_EXCEEDED = "request_cap_exceeded"
    TRANSIENT = "transient"
    UNREADABLE = "unreadable"


RUN_ABORTING_KINDS = frozenset(
    {FailureKind.MISCONFIGURED, FailureKind.RATE_LIMITED, FailureKind.REQUEST_CAP_EXCEEDED}
)


@dataclass(frozen=True)
class TranscribedItem:
    problem_id: str
    prompt_text: str
    student_answer_raw: str
    confidence: float
    bbox: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class PageIdentityExtraction:
    """Whatever page-identity markers a student capture's own photo shows, in the
    exact shape `k12ta.grading.page_identity.resolve()` already expects -- built
    here, not there, because extraction (what the model saw) and resolution (what
    to do about it) are different concerns, same split as `TranscribedItem`
    confidence versus `k12ta.grading.needs_human.decide()`."""

    candidates: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Identity kind -> every distinct value seen for it on this one photo. More
    than one value for a kind is exactly what a two-page spread with two "Day N"
    banners looks like -- preserved here, never collapsed to one, so `resolve()`
    can refuse it as CONFLICTING instead of guessing."""
    confidence: float = 0.0
    """The model's confidence in this extraction as a whole, independent of any
    single item's `TranscribedItem.confidence`."""


@dataclass(frozen=True)
class TranscriptionResult:
    items: tuple[TranscribedItem, ...]
    provider: str
    model: str
    cost_usd: float
    latency_ms: int
    data_retention: DataRetention
    """What the provider's tier permits it to do with the image just sent. Sourced from
    the adapter that produced this result, never hardcoded at the call site, so a
    free-tier run cannot silently look free."""
    failure: str | None = None
    """None on success. A short reason otherwise, so a network failure and a genuinely
    blank page are never conflated in downstream reporting."""
    failure_kind: FailureKind | None = None
    """None on success. Set alongside `failure` so a caller can decide whether to keep
    going (TRANSIENT, UNREADABLE) or abort the run (everything in RUN_ABORTING_KINDS)."""
    page_identity: PageIdentityExtraction = field(default_factory=PageIdentityExtraction)

    def min_confidence(self) -> float:
        return min((i.confidence for i in self.items), default=0.0)


class Transcriber(Protocol):
    """Anything that turns a page image into structured problems and answers."""

    name: str

    @property
    def request_count(self) -> int:
        """Total model-provider requests made so far by this transcriber, including
        retries. A run reuses one instance across every page, so this is a running
        total for the run — the cost the eval report states alongside its accuracy
        metrics. Declared read-only: implementers may expose it as a computed
        property (VisionLLMTranscriber delegates to its adapter) or a plain settable
        attribute (a mutable attribute always satisfies a read-only expectation) —
        but never as something callers are meant to assign to."""
        ...

    def transcribe(
        self, image_path: str, identity_schema: Sequence[tuple[str, str | None]] = ()
    ) -> TranscriptionResult:
        """Read one page. Must not raise; classify any failure and return it
        instead. `identity_schema` is the source's current identity components
        as `(component_name, example)` pairs, in schema position order -- empty
        when the source has no schema yet (discovery mode)."""
        ...
