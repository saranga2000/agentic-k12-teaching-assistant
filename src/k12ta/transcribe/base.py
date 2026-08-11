"""Transcriber provider abstraction.

Phase 1 has exactly one implementation (a vision LLM). The abstraction exists now so
that adding a second reader later is a new file, not a rewrite, and so that the eval
harness can score any provider through one interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TranscribedItem:
    problem_id: str
    prompt_text: str
    student_answer_raw: str
    confidence: float
    bbox: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    items: tuple[TranscribedItem, ...]
    provider: str
    model: str
    cost_usd: float
    latency_ms: int

    def min_confidence(self) -> float:
        return min((i.confidence for i in self.items), default=0.0)


class Transcriber(Protocol):
    """Anything that turns a page image into structured problems and answers."""

    name: str

    def transcribe(self, image_path: str) -> TranscriptionResult:
        """Read one page. Must not raise on unreadable input; return low confidence."""
        ...
