from __future__ import annotations

from k12ta.prompts import load_prompt, load_prompt_version


def test_loads_transcribe_page_prompt_body_without_frontmatter() -> None:
    body = load_prompt("transcribe_page")

    assert "id: transcribe_page" not in body
    assert "Return JSON only" in body
    assert body == body.strip()


def test_loads_coach_voice_prompt() -> None:
    body = load_prompt("coach_voice")

    assert "id: coach_voice" not in body
    assert "Feedback permissions" in body


def test_loads_a_prompts_version_from_its_frontmatter() -> None:
    assert load_prompt_version("coach_voice") >= 1


def test_prompt_version_is_an_int_not_a_string() -> None:
    assert isinstance(load_prompt_version("coach_voice"), int)
