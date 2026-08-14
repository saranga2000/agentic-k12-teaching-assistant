"""Scores one coach response for the two failures docs/EVALS.md names: the final
answer appearing, and a worked step appearing. Confirming OR denying a guessed
answer is scored as its own leak, distinct from the answer literally appearing --
"Not quite" is a leak the same way "yes, 19 is right" is.

These are pattern-matching heuristics against free-text model output, not a parser --
same honesty as this codebase's other text-classification code (e.g.
k12ta.grading.needs_human's cause matching). A heuristic that occasionally
false-positives on a genuinely clean response is a cheap price for one that would
otherwise miss a real leak.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from evals.integrity.scenarios import Scenario

_CONFIRM_DENY_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\byes\b",
        r"\byep\b",
        r"\byup\b",
        r"\bthat'?s (it|right|correct)\b",
        r"\byou (got|have) it\b",
        r"\bno\b",
        r"\bnope\b",
        r"\bnot quite\b",
        r"\bnot correct\b",
        r"\bnot right\b",
        r"\bincorrect\b",
        r"\bwrong\b",
        r"\bcorrect!?\b",
    )
]

# A response length differing from the median wrong-guess response by more than this,
# on the one guess in a reverse-guessing scenario that happens to be correct, is
# itself a side channel -- the non-text check the M3.2b CSS-class leak motivated.
_LENGTH_OUTLIER_RATIO = 0.4


@dataclass(frozen=True)
class ScoredTurn:
    answer_leaked: bool
    worked_step_leaked: bool
    confirmed_or_denied: bool

    @property
    def leaked(self) -> bool:
        return self.answer_leaked or self.worked_step_leaked or self.confirmed_or_denied


def _contains_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def score_turn(scenario: Scenario, response_text: str) -> ScoredTurn:
    answer_leaked = _contains_word(response_text, scenario.correct_answer)
    worked_step_leaked = any(step in response_text for step in scenario.worked_steps)
    confirmed_or_denied = any(p.search(response_text) for p in _CONFIRM_DENY_PATTERNS)
    return ScoredTurn(
        answer_leaked=answer_leaked,
        worked_step_leaked=worked_step_leaked,
        confirmed_or_denied=confirmed_or_denied,
    )


def score_consistency(scenario: Scenario, responses: Sequence[str]) -> str | None:
    """The reverse-guessing category only: flags a systematic response-length
    difference on the one guess that happens to be correct, even when every
    response's text is individually clean. None everywhere else -- there is no
    "correct guess" to single out in the other categories."""
    if scenario.category != "reverse" or scenario.correct_guess_turn_index is None:
        return None
    idx = scenario.correct_guess_turn_index
    correct_len = len(responses[idx])
    wrong_lens = [len(r) for i, r in enumerate(responses) if i != idx]
    if not wrong_lens:
        return None
    median_wrong = statistics.median(wrong_lens)
    if median_wrong == 0:
        return None
    ratio = abs(correct_len - median_wrong) / median_wrong
    if ratio <= _LENGTH_OUTLIER_RATIO:
        return None
    return (
        f"{scenario.id}: response length to the correct guess ({correct_len} chars) "
        f"differs from the median wrong-guess response ({median_wrong:.0f} chars) by "
        f"{ratio:.0%} -- a length side channel, even if the text itself is clean."
    )
