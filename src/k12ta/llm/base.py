"""Provider-agnostic vision-model interface.

Every model call in this system goes through something satisfying `VisionModel`, per
AGENTS.md rule 9. Swapping providers is a new file implementing this protocol, not a
refactor of anything that calls it.
"""

from __future__ import annotations

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


class VisionModel(Protocol):
    """Anything that turns a prompt plus one image into raw model text."""

    data_retention: DataRetention

    def generate(self, prompt: str, image_bytes: bytes, mime_type: str) -> VisionResponse:
        """Call the model once. Raises on failure; the caller decides how to degrade."""
        ...
