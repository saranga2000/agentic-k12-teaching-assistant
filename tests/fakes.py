"""Test doubles shared across test modules. Not collected as tests itself."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from k12ta.llm.base import ChatResponse, DataRetention, VisionImage, VisionResponse
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


@dataclass
class FakeTextModel:
    """A `TextModel` (k12ta.grading.evaluator's tier 2) that returns canned
    replies in order and never touches the network. One reply per call --
    k12ta.grading.evaluator.evaluate_keyless makes two calls per invocation,
    so a keyless test needs two replies queued."""

    replies: list[str] = field(default_factory=list)
    data_retention: DataRetention = DataRetention.NO_RETENTION
    request_count: int = field(default=0, init=False)
    seen_prompts: list[str] = field(default_factory=list, init=False)

    def generate_conversation(self, system_prompt: str, turns: object) -> ChatResponse:
        self.seen_prompts.append(system_prompt)
        reply = self.replies[self.request_count]
        self.request_count += 1
        return ChatResponse(text=reply, cost_usd=Decimal("0"), latency_ms=1)

    def verify(self) -> None:
        pass


@dataclass
class FakeVisionModel:
    """A `VisionModel` (k12ta.grading.evaluator's tier 3) that returns canned
    replies in order and never touches the network. One reply per call to
    generate_multi -- generate() is not implemented, since k12ta.grading.
    evaluator.evaluate_vision always calls generate_multi."""

    replies: list[str] = field(default_factory=list)
    data_retention: DataRetention = DataRetention.NO_RETENTION
    request_count: int = field(default=0, init=False)
    seen_prompts: list[str] = field(default_factory=list, init=False)
    seen_image_counts: list[int] = field(default_factory=list, init=False)

    def generate(
        self,
        prompt: str,
        image_bytes: bytes,
        mime_type: str,
        on_progress: Callable[[int], None] | None = None,
    ) -> VisionResponse:
        raise NotImplementedError("evaluate_vision always calls generate_multi")

    def generate_multi(
        self,
        prompt: str,
        images: Sequence[VisionImage],
        on_progress: Callable[[int], None] | None = None,
    ) -> VisionResponse:
        self.seen_prompts.append(prompt)
        self.seen_image_counts.append(len(images))
        reply = self.replies[self.request_count]
        self.request_count += 1
        return VisionResponse(text=reply, cost_usd=Decimal("0"), latency_ms=1)

    def verify(self) -> None:
        pass
