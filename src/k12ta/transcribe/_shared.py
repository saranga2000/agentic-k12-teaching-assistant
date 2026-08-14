"""Helpers shared by every vision-model transcriber in this package.

Not part of the public API of `k12ta.transcribe` (the leading underscore on the module
marks that); it exists so `vision_llm.py` and `key_page.py` don't each carry their own
copy of the same few lines.
"""

from __future__ import annotations

from collections.abc import Sequence

_SCHEMA_PLACEHOLDER = "{{SCHEMA_COMPONENTS}}"


def render_identity_schema_block(components: Sequence[tuple[str, str | None]]) -> str:
    """The "look for exactly these named markers" instruction block for a
    prompt's `{{SCHEMA_COMPONENTS}}` placeholder. Empty when `components` is
    empty (discovery mode -- no schema known yet for this source), which the
    surrounding prompt text is written to read as "report anything you see,
    under your own name for it" when nothing follows."""
    if not components:
        return ""
    lines = []
    for name, example in components:
        if example:
            lines.append(f'- "{name}": a marker like "{example}", as printed')
        else:
            lines.append(f'- "{name}", as printed')
    return "\n".join(lines)


def build_identity_prompt(base_prompt: str, components: Sequence[tuple[str, str | None]]) -> str:
    """Fills the base prompt's `{{SCHEMA_COMPONENTS}}` placeholder -- the one
    piece of a prompt built per call rather than loaded once, since which
    components to look for is a fact about the source being read, not something
    fixed at load time. Shared by both transcribers (`vision_llm.py`,
    `key_page.py`) so the interpolation logic exists in exactly one place."""
    return base_prompt.replace(_SCHEMA_PLACEHOLDER, render_identity_schema_block(components))


def strip_code_fence(text: str) -> str:
    """The prompt says no markdown fence, but models do not always listen."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    return stripped.strip()
