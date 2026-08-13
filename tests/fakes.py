"""Test doubles shared across test modules. Not collected as tests itself."""

from __future__ import annotations

from dataclasses import dataclass, field

from k12ta.transcribe.base import TranscriptionResult


@dataclass
class FakeTranscriber:
    """A `Transcriber` that returns a canned result and never touches the network.

    Records every `image_path` it was called with, so a test can assert it was
    called exactly once (or, for the quota-exhausted case, never at all).
    """

    name: str = "fake"
    result: TranscriptionResult | None = None
    request_count: int = field(default=0, init=False)
    calls: list[str] = field(default_factory=list, init=False)

    def transcribe(self, image_path: str) -> TranscriptionResult:
        assert self.result is not None, "set FakeTranscriber.result before calling transcribe"
        self.calls.append(image_path)
        self.request_count += 1
        return self.result
