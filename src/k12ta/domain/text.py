"""Strip LaTeX-style markup from transcribed text at display time.

`prompts/transcribe_page.md` and `prompts/transcribe_key_page.md` were fixed
(v6) to ask the model for plain text instead of LaTeX, but that only changes
*future* model calls -- rows transcribed before that fix still have `$\\frac
{3}{4}$`-style markup baked into their stored `prompt_text`/
`student_answer_raw`/`answer_text`, and the model can still reach for markup
again despite the instruction. This is the safety net: applied at render
time, never at write time, so it never rewrites what's actually stored and
covers old and new rows alike. Deliberately narrow -- it targets exactly the
patterns observed in real captures (see docs/ROADMAP.md's M3.6/M3.9 notes),
not general LaTeX parsing. Kept here, not in k12ta.respond, because both a
student surface (via k12ta.respond) and the parent-only k12ta.keys need it,
and k12ta.keys must never import k12ta.respond (tests/test_architecture_
boundaries.py) -- k12ta.domain is the shared, I/O-free base both already
depend on.
"""

from __future__ import annotations

import re

_DOLLAR_DELIMITERS = re.compile(r"\$\$?")
_LEFT_RIGHT = re.compile(r"\\left|\\right")
_TEXT_COMMAND = re.compile(r"\\text\{([^{}]*)\}")
_INNERMOST_FRAC = re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
_SIMPLE_TOKEN = re.compile(r"^[A-Za-z0-9./]+$")

_OPERATORS = {
    r"\times": "×",
    r"\div": "÷",
    r"\cdot": "·",
}


def _replace_fractions(text: str) -> str:
    """Repeatedly collapses the innermost `\\frac{A}{B}` (the one with no
    nested braces left inside it) into "A/B", which resolves nesting from the
    inside out -- a single regex can't balance nested braces, but applying the
    same innermost-only pattern until nothing matches does. A mixed number
    like "4\\frac{3}{4}" (no space before the command) becomes "4 3/4"; either
    side gets wrapped in parentheses once it stops being a single simple
    token (e.g. once it already contains a resolved "3/4" or a space), so a
    result like "(4 3/4)/10" stays unambiguous."""
    while True:
        match = _INNERMOST_FRAC.search(text)
        if not match:
            return text
        numerator, denominator = match.group(1), match.group(2)
        num_text = numerator if _SIMPLE_TOKEN.match(numerator) else f"({numerator})"
        den_text = denominator if _SIMPLE_TOKEN.match(denominator) else f"({denominator})"
        replacement = f"{num_text}/{den_text}"
        start = match.start()
        if start > 0 and text[start - 1].isalnum():
            replacement = f" {replacement}"
        text = text[:start] + replacement + text[match.end() :]


def humanize_math_text(text: str) -> str:
    """Idempotent: text with none of these patterns is returned unchanged."""
    result = _replace_fractions(text)
    result = _TEXT_COMMAND.sub(r"\1", result)
    result = _LEFT_RIGHT.sub("", result)
    for command, symbol in _OPERATORS.items():
        result = result.replace(command, symbol)
    result = _DOLLAR_DELIMITERS.sub("", result)
    return result
