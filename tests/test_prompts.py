from __future__ import annotations

from k12ta.prompts import load_prompt


def test_loads_transcribe_page_prompt_body_without_frontmatter() -> None:
    body = load_prompt("transcribe_page")

    assert "id: transcribe_page" not in body
    assert "Return JSON only" in body
    assert body == body.strip()


def test_loads_coach_voice_prompt() -> None:
    body = load_prompt("coach_voice")

    assert "id: coach_voice" not in body
    assert "Feedback permissions" in body
