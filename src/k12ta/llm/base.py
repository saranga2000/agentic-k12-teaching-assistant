"""Provider-agnostic vision-model interface.

Every model call in this system goes through something satisfying `VisionModel`, per
AGENTS.md rule 9. Swapping providers is a new file implementing this protocol, not a
refactor of anything that calls it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Protocol


class DataRetention(Enum):
    """What a provider's tier permits it to do with submitted content.

    Not a technical detail: it travels on every result an adapter produces so that a
    cost report stated only in dollars cannot make a data-funded free tier look free.
    """

    PROVIDER_MAY_TRAIN = "provider_may_train"
    NO_RETENTION = "no_retention"


@dataclass(frozen=True)
class VisionResponse:
    text: str
    cost_usd: Decimal
    latency_ms: int


class ModelCallError(RuntimeError):
    """Base class for a model-call failure an adapter has classified.

    Defined here, not in a specific adapter, so that k12ta.transcribe can react to why
    a call failed without importing a provider's HTTP details (AGENTS.md rule 9 confines
    those to k12ta.llm). Any exception an adapter raises that is not one of these
    subclasses is treated as an unclassified per-page failure.
    """


class MisconfiguredError(ModelCallError):
    """The request was never valid: bad model name, bad or missing key. Every
    subsequent call will fail identically, so the caller should abort the whole run
    rather than continue to the next page."""


class RateLimitExhaustedError(ModelCallError):
    """The adapter retried a rate limit until its own retry budget was spent. One page
    exhausting retries is evidence about the account's quota, not about that page — the
    caller should abort the whole run rather than repeat the same loss on every
    remaining page."""


class TransientError(ModelCallError):
    """A transient failure (e.g. a 5xx) worth recording as a page failure, but not
    grounds to distrust the rest of the run."""


class RequestCapExceededError(ModelCallError):
    """The run's configured request ceiling, including retries, was reached. Raised
    before the request that would exceed it is sent."""


class VisionModel(Protocol):
    """Anything that turns a prompt plus one image into raw model text."""

    data_retention: DataRetention
    request_count: int
    """Total HTTP requests made so far by this instance, including retries. A run
    reuses one instance across every page, so this is a running total for the run."""

    def generate(
        self,
        prompt: str,
        image_bytes: bytes,
        mime_type: str,
        on_progress: Callable[[int], None] | None = None,
    ) -> VisionResponse:
        """Call the model once. Raises on failure; the caller decides how to degrade.
        `on_progress`, if given, is called with the cumulative character count
        received so far -- a caller with nothing better than a static spinner uses
        this to show something honest about a call that can run minutes."""
        ...

    def verify(self) -> None:
        """Make one cheap call confirming the configured model exists and the key
        works. Raises MisconfiguredError or RateLimitExhaustedError on failure; raises
        nothing on success. Intended to run once, before any page is sent."""
        ...
