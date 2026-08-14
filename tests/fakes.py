"""Test doubles shared across test modules. Not collected as tests itself."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from k12ta.transcribe.base import TranscriptionResult
from k12ta.transcribe.key_page import KeyPageResult


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
    identity_schemas_seen: list[Sequence[tuple[str, str | None]]] = field(
        default_factory=list, init=False
    )
    """Every `identity_schema` this fake was called with, in order -- so a test
    can assert the pipeline actually loaded and passed a source's real schema,
    not just that transcribe() was called."""

    def transcribe(
        self, image_path: str, identity_schema: Sequence[tuple[str, str | None]] = ()
    ) -> TranscriptionResult:
        assert self.result is not None, "set FakeTranscriber.result before calling transcribe"
        self.calls.append(image_path)
        self.request_count += 1
        self.identity_schemas_seen.append(identity_schema)
        return self.result


@dataclass
class FakeKeyTranscriber:
    """A `KeyTranscriber` that returns a canned result and never touches the network.

    Records every call's `image_bytes`, so a test can assert it was called exactly
    once (or, for the quota-exhausted case, never at all).
    """

    name: str = "fake_key"
    result: KeyPageResult | None = None
    progress_updates: tuple[int, ...] = ()
    """Chars to report via on_progress, in order, before returning -- stands in for
    a real streamed call's chunks arriving."""
    request_count: int = field(default=0, init=False)
    calls: list[bytes] = field(default_factory=list, init=False)
    identity_schemas_seen: list[Sequence[tuple[str, str | None]]] = field(
        default_factory=list, init=False
    )

    def transcribe(
        self,
        image_bytes: bytes,
        on_progress: Callable[[int], None] | None = None,
        identity_schema: Sequence[tuple[str, str | None]] = (),
    ) -> KeyPageResult:
        assert self.result is not None, "set FakeKeyTranscriber.result before calling transcribe"
        self.calls.append(image_bytes)
        self.request_count += 1
        self.identity_schemas_seen.append(identity_schema)
        if on_progress is not None:
            for chars in self.progress_updates:
                on_progress(chars)
        return self.result
