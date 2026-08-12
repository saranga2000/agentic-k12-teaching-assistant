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
