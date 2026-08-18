"""Loads versioned prompts from prompts/*.md.

Never write a prompt string inline in Python (AGENTS.md rule 7). Prompts are versioned
and eval'd like code.
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def load_prompt(prompt_id: str) -> str:
    """Read prompts/<prompt_id>.md and return its body, with frontmatter stripped."""
    text = (PROMPTS_DIR / f"{prompt_id}.md").read_text()
    parts = text.split("---", 2)
    if len(parts) == 3:
        return parts[2].strip()
    return text.strip()


def load_prompt_version(prompt_id: str) -> int:
    """Read the `version:` field out of prompts/<prompt_id>.md's frontmatter.

    A recording of this prompt's behaviour (see evals/integrity/) is only honest
    against the exact text that produced it -- this is what lets a caller stamp
    a recording with the version it was made under and refuse to trust it once
    the prompt has moved on, the same staleness rule
    k12ta.store.page_identity_schemas already applies to a page-identity schema."""
    text = (PROMPTS_DIR / f"{prompt_id}.md").read_text()
    parts = text.split("---", 2)
    if len(parts) != 3:
        raise ValueError(f"prompts/{prompt_id}.md has no frontmatter to read a version from")
    for line in parts[1].strip().splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "version":
            return int(value.strip())
    raise ValueError(f"prompts/{prompt_id}.md's frontmatter has no version: field")
