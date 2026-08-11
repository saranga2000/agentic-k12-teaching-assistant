"""Choose what to practise next.

Selection is explicitly not 'weakest first'. A session made entirely of the worst
skills is demoralising and produces avoidance. The shape is: one winnable opener, the
due-and-weak skills in the middle, one winnable closer.

Corollary: when few skills are due, the session gets shorter rather than being padded
with already-mastered work. At most two items in any session come from the solid pile,
because practising things the student already knows is how a tool starts feeling like
busywork.
"""

from __future__ import annotations

from datetime import date

from alc.mastery.model import SkillMastery

MAX_SOLID_ITEMS = 2
"""One opener and one closer. Never more."""


def select_for_session(
    traces: list[SkillMastery],
    on: date,
    max_items: int = 6,
    weak_share: float = 0.6,
) -> list[SkillMastery]:
    """Return an ordered practice list for one session.

    Guarantees, in order of priority:
    1. Every returned item is distinct.
    2. At most `MAX_SOLID_ITEMS` come from skills that are not due.
    3. If any solid item is included, the first item is one, so the session opens with
       something the student is likely to get right.
    4. Length is `min(max_items, len(due) + solid_used)`, never padded beyond that.
    """
    if max_items <= 0:
        return []

    due = sorted((t for t in traces if t.is_due(on)), key=lambda t: t.retention_on(on))
    solid = sorted(
        (t for t in traces if not t.is_due(on)),
        key=lambda t: t.retention_on(on),
        reverse=True,
    )

    solid_budget = min(MAX_SOLID_ITEMS, len(solid), max_items)
    weak_budget = max_items - solid_budget
    middle = due[:weak_budget]

    # If there is little due work, spend the slack on the opener and closer only.
    if len(middle) < weak_budget:
        solid_budget = min(MAX_SOLID_ITEMS, len(solid), max_items - len(middle))

    chosen_solid = solid[:solid_budget]
    opener = chosen_solid[:1]
    closer = chosen_solid[1:2]
    return opener + middle + closer
