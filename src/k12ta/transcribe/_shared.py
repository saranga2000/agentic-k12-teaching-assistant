"""Helpers shared by every vision-model transcriber in this package.

Not part of the public API of `k12ta.transcribe` (the leading underscore on the module
marks that); it exists so `vision_llm.py` and `key_page.py` don't each carry their own
copy of the same few lines.
"""

from __future__ import annotations


def strip_code_fence(text: str) -> str:
    """The prompt says no markdown fence, but models do not always listen."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    return stripped.strip()
