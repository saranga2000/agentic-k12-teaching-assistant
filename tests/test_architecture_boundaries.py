"""Import-boundary invariants from docs/ARCHITECTURE.md that no type checker
enforces on its own: k12ta.respond (student-facing rendering) and k12ta.keys
(parent-only screens) must never import each other, in either direction."""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src" / "k12ta"


def _references(package_dir: Path, target_module: str) -> list[str]:
    hits = []
    for path in package_dir.rglob("*.py"):
        text = path.read_text()
        if re.search(
            rf"^\s*(?:import|from)\s+{re.escape(target_module)}(?:\.|(?:\s|$))", text, re.MULTILINE
        ):
            hits.append(str(path))
    return hits


def test_keys_never_imports_respond() -> None:
    """Parent surfaces render full detail, unfiltered, on purpose -- a student
    renderer must never end up reachable from k12ta.keys."""
    hits = _references(_SRC / "keys", "k12ta.respond")
    assert hits == []


def test_respond_never_imports_keys() -> None:
    """The reverse boundary: k12ta.respond must never reach into the parent-only
    app, so a parent-only helper can't accidentally end up filtered for a
    student surface either."""
    hits = _references(_SRC / "respond", "k12ta.keys")
    assert hits == []
