"""Score a Transcriber against hand-labelled fixtures.

Run before implementing any transcriber, so that the first number is honest.

    python evals/run_transcription_eval.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures"
CONFIDENCE_BANDS = [(0.95, 1.01), (0.85, 0.95), (0.0, 0.85)]


@dataclass
class Scorecard:
    pages: int = 0
    expected_items: int = 0
    detected_items: int = 0
    exact_matches: int = 0
    band_totals: dict[str, int] | None = None
    band_correct: dict[str, int] | None = None

    def detection_recall(self) -> float:
        return self.detected_items / self.expected_items if self.expected_items else 0.0

    def exact_match_rate(self) -> float:
        return self.exact_matches / self.detected_items if self.detected_items else 0.0

    def render(self) -> str:
        lines = [
            f"pages              {self.pages}",
            f"detection recall   {self.detection_recall():.3f}",
            f"answer exact match {self.exact_match_rate():.3f}",
            "calibration:",
        ]
        for band, total in (self.band_totals or {}).items():
            correct = (self.band_correct or {}).get(band, 0)
            rate = correct / total if total else 0.0
            lines.append(f"  {band:>12}  n={total:<4} accuracy={rate:.3f}")
        return "\n".join(lines)


def load_fixtures() -> list[dict[str, object]]:
    return [json.loads(p.read_text()) for p in sorted(FIXTURE_DIR.glob("*.json"))]


def main() -> None:
    fixtures = load_fixtures()
    if not fixtures:
        print(
            "No fixtures found.\n"
            "M1 starts here: photograph 40 to 60 real pages and label them.\n"
            "See evals/fixtures/README.md."
        )
        return
    print(f"{len(fixtures)} fixture pages loaded.")
    print("Implement VisionLLMTranscriber, then wire it in here and score it.")


if __name__ == "__main__":
    main()
