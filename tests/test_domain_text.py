"""humanize_math_text strips LaTeX markup a model emitted before prompt v6
(or might still emit by accident) so a parent or student never reads raw
markup instead of the plain answer it was meant to represent."""

from __future__ import annotations

from k12ta.domain.text import humanize_math_text


def test_plain_text_is_unchanged() -> None:
    assert humanize_math_text("3/4") == "3/4"
    assert humanize_math_text("Jahnvi wrote 0.475") == "Jahnvi wrote 0.475"


def test_simple_dollar_fraction() -> None:
    assert humanize_math_text(r"$\frac{3}{4}$") == "3/4"


def test_text_command_with_unit() -> None:
    assert humanize_math_text(r"$5\text{ ft}$") == "5 ft"


def test_mixed_number() -> None:
    assert humanize_math_text(r"$4\frac{3}{4}$ cups") == "4 3/4 cups"


def test_nested_fraction_from_real_capture() -> None:
    # The exact screenshot case: a chef-broth ratio problem with a fraction
    # inside a fraction's numerator.
    text = r"equivalent ratios: $\frac{4\frac{3}{4}}{10} = \frac{x}{1}$"
    assert humanize_math_text(text) == "equivalent ratios: (4 3/4)/10 = x/1"


def test_operators() -> None:
    assert humanize_math_text(r"$6 \times 7 \div 2 \cdot 3$") == "6 × 7 ÷ 2 · 3"


def test_left_right_delimiters_stripped() -> None:
    assert humanize_math_text(r"$\left(3 + 4\right)$") == "(3 + 4)"


def test_idempotent() -> None:
    once = humanize_math_text(r"$4\frac{3}{4}$ cups")
    assert humanize_math_text(once) == once
