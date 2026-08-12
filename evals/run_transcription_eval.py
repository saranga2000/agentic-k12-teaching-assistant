"""Score a Transcriber against hand-labelled fixtures.

Run before implementing any transcriber, so that the first number is honest.

    python evals/run_transcription_eval.py
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from k12ta.config import Settings
from k12ta.evals.fixtures import FixtureItem, Layout, load_fixture_pages
from k12ta.grading.key_grader import normalise
from k12ta.llm import build_vision_model
from k12ta.llm.base import DataRetention
from k12ta.transcribe.base import TranscribedItem, Transcriber
from k12ta.transcribe.vision_llm import VisionLLMTranscriber

FIXTURE_DIR = Path(__file__).parent / "fixtures"
RESULTS_DIR = Path(__file__).parent / "results"
CONFIDENCE_BANDS = [(0.95, 1.01), (0.85, 0.95), (0.0, 0.85)]


def _band_label(low: float, high: float) -> str:
    return f"{low:.2f}-{high:.2f}"


def _band_for(confidence: float) -> str:
    for low, high in CONFIDENCE_BANDS:
        if low <= confidence < high:
            return _band_label(low, high)
    return _band_label(*CONFIDENCE_BANDS[-1])


@dataclass
class Scorecard:
    """Accumulated detection and transcription-accuracy counts for one slice.

    `matched_items` are id-matched to a fixture item. `misnumbered_items` are matched
    only by a normalised prompt_text fallback (the problem was found, its printed number
    was misread) and are deliberately excluded from exact-match and calibration: those
    metrics are about answer-reading fidelity on an item we are confident we identified
    correctly, and a misnumbering already signals we are not confident of that.

    `spurious_items` are unmatched detections on a page whose whole image was labelled,
    so we know they don't correspond to a real problem: a model failure. `unattributed_
    items` are the same kind of unmatched detection, but on a `two-page-spread` page
    where only one side was labelled — the detection may be invented, or it may be a
    correct reading of the unlabelled side, and the fixture cannot say which. It is
    excluded from `spurious_items`, from `detection_recall`, and from
    `detection_precision`; it is reported on its own so it is never mistaken for a model
    failure.
    """

    pages: int = 0
    expected_items: int = 0
    matched_items: int = 0
    misnumbered_items: int = 0
    spurious_items: int = 0
    unattributed_items: int = 0
    exact_matches: int = 0
    band_totals: dict[str, int] = field(default_factory=dict)
    band_correct: dict[str, int] = field(default_factory=dict)

    def detection_recall(self) -> float:
        """Fraction of expected problems found, under any identified number."""
        found = self.matched_items + self.misnumbered_items
        return found / self.expected_items if self.expected_items else 0.0

    def detection_precision(self) -> float:
        """Fraction of reported problems that correspond to a real problem on the page."""
        found = self.matched_items + self.misnumbered_items
        denominator = found + self.spurious_items
        return found / denominator if denominator else 0.0

    def exact_match_rate(self) -> float:
        """Answer-transcription accuracy on cleanly id-matched items only."""
        return self.exact_matches / self.matched_items if self.matched_items else 0.0

    def calibration(self) -> dict[str, float]:
        return {
            band: (self.band_correct.get(band, 0) / total if total else 0.0)
            for band, total in self.band_totals.items()
        }

    def render(self, title: str) -> str:
        found = self.matched_items + self.misnumbered_items
        lines = [
            f"### {title}",
            "",
            f"- pages: {self.pages}",
            f"- expected items: {self.expected_items}",
            f"- detection recall: {self.detection_recall():.3f} ({found}/{self.expected_items})",
            f"- detection precision: {self.detection_precision():.3f}",
            f"- misnumbered (right problem, wrong printed number): {self.misnumbered_items}",
            f"- spurious (problem not on the page): {self.spurious_items}",
            f"- unattributed (found on the unlabelled side of a two-page spread, "
            f"could be real or invented): {self.unattributed_items}",
            f"- answer exact match: {self.exact_match_rate():.3f} "
            f"({self.exact_matches}/{self.matched_items}, matched items only)",
            "- calibration:",
        ]
        for band in sorted(self.band_totals, reverse=True):
            total = self.band_totals[band]
            correct = self.band_correct.get(band, 0)
            rate = correct / total if total else 0.0
            lines.append(f"  - {band}: n={total} accuracy={rate:.3f}")
        return "\n".join(lines)


@dataclass(frozen=True)
class FailedPage:
    """A page the transcriber could not score at all, kept apart from a page it read
    and scored zero on — conflating the two would make a bad network evening look like
    a model regression."""

    page_id: str
    reason: str


@dataclass
class EvalReport:
    overall: Scorecard
    by_device: dict[str, Scorecard]
    by_method: dict[str, Scorecard]
    by_layout: dict[str, Scorecard]
    by_source: dict[str, Scorecard]
    data_retention: DataRetention | None
    failed_pages: list[FailedPage] = field(default_factory=list)

    def to_markdown(self, transcriber_name: str, run_at: datetime) -> str:
        lines = [
            f"# Transcription eval: {transcriber_name}",
            "",
            f"Run at {run_at.isoformat(timespec='minutes')}.",
        ]
        if self.data_retention is not None:
            lines.append(
                f"Data retention: {self.data_retention.value} (see docs/DATA_POLICY.md)."
            )
        lines.append("")
        if self.failed_pages:
            lines.append(f"## Failed pages ({len(self.failed_pages)}, excluded from scoring)")
            lines.append("")
            for failed in self.failed_pages:
                lines.append(f"- {failed.page_id}: {failed.reason}")
            lines.append("")
        lines.append(self.overall.render("Overall"))
        lines.append("")
        for title, slices in (
            ("By capture device", self.by_device),
            ("By capture method", self.by_method),
            ("By layout", self.by_layout),
            ("By source", self.by_source),
        ):
            if not slices:
                continue
            lines.append(f"## {title}")
            lines.append("")
            for key in sorted(slices):
                lines.append(slices[key].render(key))
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"


@dataclass
class _PageMatch:
    matched: list[tuple[FixtureItem, TranscribedItem]]
    misnumbered_count: int
    spurious_count: int
    unattributed_count: int


def _normalise_prompt(text: str) -> str:
    """Casefold and collapse whitespace only. Unlike key_grader.normalise, this keeps
    punctuation and spacing that distinguish one problem statement from another; it
    exists for fuzzy-matching prompt text, not for comparing answers."""
    return " ".join(text.casefold().split())


def _find_prompt_match(item: FixtureItem, candidates: list[TranscribedItem]) -> int | None:
    target = _normalise_prompt(item.prompt_text)
    for index, candidate in enumerate(candidates):
        if _normalise_prompt(candidate.prompt_text) == target:
            return index
    return None


def _match_page(
    expected: tuple[FixtureItem, ...], transcribed: tuple[TranscribedItem, ...], layout: Layout
) -> _PageMatch:
    """Match transcribed items to expected items, primarily by problem_id.

    Anything left over on both sides gets a second chance on normalised prompt_text,
    which distinguishes "the transcriber missed this problem" from "the transcriber
    found it but misread its printed number" — two different failure modes that would
    otherwise both look like a plain miss plus a plain spurious detection.

    Detections still left over after that are spurious on a `single-page` page, where
    the whole image was labelled and no real problem can explain them. On a
    `two-page-spread` page, only one side was labelled, so a leftover detection may be
    a correct reading of the unlabelled side rather than an invention; it is counted as
    unattributed instead, never spurious.
    """
    transcribed_by_id = {t.problem_id: t for t in transcribed}
    matched: list[tuple[FixtureItem, TranscribedItem]] = []
    unmatched_expected: list[FixtureItem] = []

    for item in expected:
        found = transcribed_by_id.pop(item.problem_id, None)
        if found is not None:
            matched.append((item, found))
        else:
            unmatched_expected.append(item)

    unmatched_transcribed = list(transcribed_by_id.values())
    misnumbered_count = 0
    for item in unmatched_expected:
        fallback_index = _find_prompt_match(item, unmatched_transcribed)
        if fallback_index is not None:
            unmatched_transcribed.pop(fallback_index)
            misnumbered_count += 1

    leftover_count = len(unmatched_transcribed)
    if layout is Layout.TWO_PAGE_SPREAD:
        spurious_count, unattributed_count = 0, leftover_count
    else:
        spurious_count, unattributed_count = leftover_count, 0

    return _PageMatch(
        matched=matched,
        misnumbered_count=misnumbered_count,
        spurious_count=spurious_count,
        unattributed_count=unattributed_count,
    )


def _accumulate_matched(
    card: Scorecard, expected_item: FixtureItem, transcribed_item: TranscribedItem
) -> None:
    card.matched_items += 1
    band = _band_for(transcribed_item.confidence)
    card.band_totals[band] = card.band_totals.get(band, 0) + 1
    transcribed_answer = normalise(transcribed_item.student_answer_raw)
    if transcribed_answer == normalise(expected_item.student_answer_raw):
        card.exact_matches += 1
        card.band_correct[band] = card.band_correct.get(band, 0) + 1


def score(transcriber: Transcriber, fixtures_dir: Path = FIXTURE_DIR) -> EvalReport:
    """Run `transcriber` against every labelled page under `fixtures_dir` and score it.

    A page whose result carries a `failure` is excluded from every scorecard and
    reported separately: a network failure and a page correctly read as having zero
    problems must never be conflated into the same zero.
    """
    overall = Scorecard()
    by_device: dict[str, Scorecard] = {}
    by_method: dict[str, Scorecard] = {}
    by_layout: dict[str, Scorecard] = {}
    by_source: dict[str, Scorecard] = {}
    failed_pages: list[FailedPage] = []
    data_retention: DataRetention | None = None

    for page in load_fixture_pages(fixtures_dir):
        result = transcriber.transcribe(str(fixtures_dir / page.image))
        data_retention = result.data_retention

        if result.failure is not None:
            failed_pages.append(FailedPage(page_id=page.page_id, reason=result.failure))
            continue

        page_match = _match_page(page.items, result.items, page.layout)

        device_card = by_device.setdefault(page.capture_device, Scorecard())
        method_card = by_method.setdefault(page.capture_method.value, Scorecard())
        layout_card = by_layout.setdefault(page.layout.value, Scorecard())
        source_card = by_source.setdefault(page.source_id, Scorecard())

        for card in (overall, device_card, method_card, layout_card, source_card):
            card.pages += 1
            card.expected_items += len(page.items)
            card.misnumbered_items += page_match.misnumbered_count
            card.spurious_items += page_match.spurious_count
            card.unattributed_items += page_match.unattributed_count
            for expected_item, transcribed_item in page_match.matched:
                _accumulate_matched(card, expected_item, transcribed_item)

    return EvalReport(
        overall=overall,
        by_device=by_device,
        by_method=by_method,
        by_layout=by_layout,
        by_source=by_source,
        data_retention=data_retention,
        failed_pages=failed_pages,
    )


def _slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", text).strip("-") or "transcriber"


def write_report(
    report: EvalReport,
    transcriber_name: str,
    results_dir: Path = RESULTS_DIR,
    run_at: datetime | None = None,
) -> Path:
    """Write a dated, timestamped report. Every run gets its own file, never overwritten."""
    run_at = run_at or datetime.now()
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = run_at.strftime("%Y-%m-%d-%H%M")
    report_path = results_dir / f"{stamp}-{_slugify(transcriber_name)}.md"
    report_path.write_text(report.to_markdown(transcriber_name, run_at))
    return report_path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Real env vars already set take precedence. No new
    dependency (python-dotenv) for a handful of KEY=value lines."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    _load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    pages = load_fixture_pages(FIXTURE_DIR)
    if not pages:
        print(
            "No labelled fixtures found.\n"
            "M1 starts here: photograph 40 to 60 real pages and label them.\n"
            "See evals/fixtures/README.md."
        )
        return

    settings = Settings.from_env()
    vision_model = build_vision_model(settings)
    transcriber = VisionLLMTranscriber(
        vision_model, provider=settings.llm_provider, model=settings.llm_model
    )

    report = score(transcriber, FIXTURE_DIR)
    run_at = datetime.now()
    print(report.to_markdown(transcriber.name, run_at))
    report_path = write_report(report, transcriber.name, run_at=run_at)
    print(f"Report written to {report_path}")


if __name__ == "__main__":
    main()
