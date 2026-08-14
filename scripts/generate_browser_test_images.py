"""Generate the synthetic JPEGs tests/browser/images/ uses as upload fixtures.

Run once, by hand, whenever an image needs to change: `python
scripts/generate_browser_test_images.py`. The output is committed to git, not
regenerated per test run, so tests/browser/ never depends on a font being installed
in CI.

Both images are computer-drawn text, not a photograph of anyone's homework -- not
covered by docs/DATA_POLICY.md's "image of a child" concern, the same reasoning
tests/test_keys_app.py already relies on for its in-memory PIL fixture.

What key_page_dense.jpg does and does not prove: it is a genuinely dense two-column
page (many small rows of text, print-scale, two columns) so it exercises the real
multipart upload and ingest path with a real, sizeable file, not a token 10x10 pixel
stand-in. It does NOT and cannot exercise anything about real transcription latency
or a real 5xx rate: tests/browser/ always stubs the model call (see
tests/browser/conftest.py), so no real model ever reads this image's content. The
timeout/retry/backoff behavior those tests cover is driven entirely by the stub's
configured delay or canned failure, never by what's actually drawn here. Proving
that a genuinely dense page is slow or 503-prone against the real API is
evals/run_transcription_eval.py's job, not this suite's.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).parent.parent / "tests" / "browser" / "images"

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _single_page() -> Image.Image:
    """A normal, single accepted student page: portrait, bright, sparse."""
    image = Image.new("RGB", (1200, 1600), color=(245, 245, 240))
    draw = ImageDraw.Draw(image)
    font = _font(36)
    draw.text((80, 100), "Summer Bridge -- Page 12", fill=(20, 20, 20), font=font)
    problems = [
        ("1.  14 + 7 = ", "21"),
        ("2.  30 - 12 = ", "18"),
        ("3.  6 x 8 = ", "48"),
    ]
    y = 260
    for prompt, answer in problems:
        draw.text((80, y), prompt + answer, fill=(20, 20, 20), font=font)
        y += 120
    return image


def _key_page_dense() -> Image.Image:
    """A genuinely dense two-column answer key: many small rows, two columns,
    print-scale text -- structurally close to a real Summer Bridge / RSM key page,
    not a token placeholder. See the module docstring for what this can and cannot
    prove."""
    image = Image.new("RGB", (1400, 1900), color=(250, 250, 247))
    draw = ImageDraw.Draw(image)
    header_font = _font(30)
    row_font = _font(20)
    draw.text((70, 60), "Answer Key -- Day 14 / Pages 27-28", fill=(15, 15, 15), font=header_font)

    left_x, right_x = 90, 760
    col_width_rows = 42
    y0 = 140
    row_height = 40
    answers = [
        "8 m",
        "15 cm",
        "answers vary",
        "4,019",
        "3/4",
        "1.25",
        "62",
        "graph or table",
        "7 ft",
        "0.5",
        "118",
        "44",
        "9 in",
        "answers vary",
        "2/3",
        "36",
    ]
    for col_x in (left_x, right_x):
        for row in range(col_width_rows):
            problem_num = row + 1 + (0 if col_x == left_x else col_width_rows)
            answer = answers[row % len(answers)]
            line = f"{problem_num}.  {answer}"
            draw.text((col_x, y0 + row * row_height), line, fill=(15, 15, 15), font=row_font)

    draw.text(
        (70, y0 + col_width_rows * row_height + 30),
        "CH.4    Page 27 of 42",
        fill=(90, 90, 90),
        font=row_font,
    )
    return image


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _single_page().save(OUT_DIR / "single_page.jpg", format="JPEG", quality=90)
    _key_page_dense().save(OUT_DIR / "key_page_dense.jpg", format="JPEG", quality=90)
    for name in ("single_page.jpg", "key_page_dense.jpg"):
        path = OUT_DIR / name
        print(f"{path}  ({path.stat().st_size} bytes, {Image.open(path).size})")


if __name__ == "__main__":
    main()
