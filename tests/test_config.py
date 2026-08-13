"""The minimal .env loader shared by k12ta.web and evals/run_transcription_eval.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from k12ta.config import load_dotenv


def test_load_dotenv_sets_variables_from_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("K12TA_TEST_PROBE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("K12TA_TEST_PROBE=from-dotenv\n")

    load_dotenv(env_file)

    assert os.environ["K12TA_TEST_PROBE"] == "from-dotenv"


def test_load_dotenv_ignores_comments_and_blank_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("K12TA_TEST_PROBE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("# a comment\n\nK12TA_TEST_PROBE=value\n")

    load_dotenv(env_file)

    assert os.environ["K12TA_TEST_PROBE"] == "value"


def test_load_dotenv_never_overrides_an_already_set_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("K12TA_TEST_PROBE", "from-real-environment")
    env_file = tmp_path / ".env"
    env_file.write_text("K12TA_TEST_PROBE=from-dotenv\n")

    load_dotenv(env_file)

    assert os.environ["K12TA_TEST_PROBE"] == "from-real-environment"


def test_load_dotenv_is_a_no_op_when_the_file_does_not_exist(tmp_path: Path) -> None:
    load_dotenv(tmp_path / "does-not-exist.env")  # must not raise
