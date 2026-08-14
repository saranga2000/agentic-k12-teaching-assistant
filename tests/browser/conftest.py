"""Browser-driven UI tests: a real Chromium (via Playwright) against a real FastAPI
server (uvicorn, in a background thread of this same process) with a real SQLite
file on disk and real uploaded image files from tests/browser/images/. The model
call is the one thing stubbed -- see `stub_web_transcriber` and
`stub_key_transcriber` below -- so every test here costs no API quota and is
deterministic. Everything between a click and the rendered page is real: real HTTP,
real ASGI routing, real Jinja2 rendering, real client-side JavaScript execution,
real SQLite writes.

Excluded from the default `pytest -q` / `make check` run (see the `browser` marker
in pyproject.toml) because it needs Chromium installed via `playwright install
chromium`, which most edits don't touch and shouldn't have to pay for. Run with
`make check-browser`, or headed to watch one:

    .venv/bin/pytest tests/browser/test_key_upload_flow.py --headed --slowmo 500

WHAT THIS SUITE DOES NOT CATCH -- read this before a green run here starts feeling
like proof the app works on a real device. It does not, and nothing added later in
this style will either, unless the gap itself changes:

- **The real camera handoff.** `capture="environment"` is a hint mobile browsers
  use to prefer opening the camera; Playwright's `set_input_files()` sets a file on
  the input directly and never touches that handoff at all. The WebKit
  standalone-PWA bug that once broke the camera entirely (docs/PROGRESS.md, M2) was
  found on a real device and would not have been caught here.
- **Real network conditions.** Everything here runs over loopback. There is no real
  WiFi flakiness, no real router idle-timeout, none of the actual connection-level
  behaviour that made the key-upload bug ("connection interrupted") visible in the
  first place. This suite proves the server now behaves correctly under a slow or
  failing model call; it does not prove a real phone on real WiFi experiences it
  the same way.
- **Real model behaviour.** The model call is always stubbed here, by design, for
  cost and determinism -- see `key_page_dense.jpg`'s docstring in
  scripts/generate_browser_test_images.py for why even the one deliberately dense
  fixture image can't change that. Prompt regressions, real transcription accuracy,
  and the actual 5xx rate on a real dense page are
  evals/run_transcription_eval.py's job (docs/EVALS.md), not this suite's.
- **Perceptual/visual quality.** Playwright asserts DOM state and CSS classes, not
  whether something reads as visually distinct to a human eye. The framing-guide
  symmetry bug (both examples once looked equally valid) is only caught here if it
  happens to also break a DOM-level assertion.
- **Real device and OS specifics.** Chromium headless approximates a viewport; it
  is not Mobile Safari, and won't reproduce an iOS-specific rendering, gesture, or
  PWA-launch-mode bug.

A green run here means the server-rendered contract and the client-side JS between
a click and a result are both doing what they're supposed to, in a real browser. It
does not mean "this works on my kid's iPad" -- an occasional real-device pass is
still worth doing, especially after touching anything camera-, capture-, or
PWA-launch-related.
"""

from __future__ import annotations

import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from k12ta.config import Settings
from k12ta.store import db, migrate
from tests.fakes import FakeKeyTranscriber, FakeTranscriber

IMAGES_DIR = Path(__file__).parent / "images"
SINGLE_PAGE_IMAGE = IMAGES_DIR / "single_page.jpg"
KEY_PAGE_DENSE_IMAGE = IMAGES_DIR / "key_page_dense.jpg"


@dataclass
class LiveServer:
    base_url: str
    settings: Settings

    def connection(self) -> sqlite3.Connection:
        """A fresh connection to the same on-disk database file the server uses.
        Never share one connection between the test and the server -- the server
        opens its own per request; this is for the test's own seed/assert steps."""
        return db.connect(str(self.settings.data_dir / "k12ta.db"))


@dataclass
class DelayedTranscriber:
    """Wraps a Fake*Transcriber and sleeps before delegating, so a test can assert
    the working-state UI is actually visible while a request is genuinely still in
    flight -- a real server-thread sleep, not a fake clock, because the assertion
    being proven is about wall-clock browser behaviour."""

    inner: FakeTranscriber | FakeKeyTranscriber
    delay_seconds: float = 0.4

    def transcribe(
        self, arg: object, on_progress: object = None, identity_schema: object = ()
    ) -> object:
        time.sleep(self.delay_seconds)
        # Only FakeKeyTranscriber's transcribe() accepts on_progress -- the student
        # capture flow isn't wired for progress reporting (out of scope; see
        # docs/ROADMAP.md's M2 note, this is the key-upload path's fix only).
        if isinstance(self.inner, FakeKeyTranscriber):
            return self.inner.transcribe(  # type: ignore[arg-type]
                arg, on_progress=on_progress, identity_schema=identity_schema
            )
        return self.inner.transcribe(arg, identity_schema=identity_schema)  # type: ignore[arg-type]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_live_server(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[LiveServer]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("K12TA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("K12TA_DAILY_REQUEST_LIMIT", "20")

    settings = Settings(
        llm_provider="anthropic",
        llm_api_key="",
        llm_model="",
        llm_max_requests_per_run=40,
        data_dir=data_dir,
        coach_name="Coach",
        daily_token_budget_usd=Decimal("1.50"),
        daily_request_limit=20,
        log_level="WARNING",
    )
    conn = db.connect(str(data_dir / "k12ta.db"))
    migrate.apply_migrations(conn)
    conn.close()

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            httpx.get(base_url, timeout=0.5)
            break
        except httpx.TransportError:
            time.sleep(0.05)
    else:
        raise RuntimeError(f"live server on {base_url} never became ready")

    try:
        yield LiveServer(base_url=base_url, settings=settings)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def web_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LiveServer]:
    import k12ta.web.app as web_app_module

    yield from _start_live_server(web_app_module.app, monkeypatch, tmp_path)


@pytest.fixture
def keys_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LiveServer]:
    import k12ta.keys.app as keys_app_module

    yield from _start_live_server(keys_app_module.app, monkeypatch, tmp_path)


@pytest.fixture
def stub_web_transcriber(monkeypatch: pytest.MonkeyPatch) -> FakeTranscriber:
    import k12ta.web.app as web_app_module

    fake = FakeTranscriber()
    monkeypatch.setattr(web_app_module, "get_transcriber", lambda settings: fake)
    return fake


@pytest.fixture
def stub_key_transcriber(monkeypatch: pytest.MonkeyPatch) -> FakeKeyTranscriber:
    import k12ta.keys.app as keys_app_module

    fake = FakeKeyTranscriber()
    monkeypatch.setattr(keys_app_module, "get_transcriber", lambda settings: fake)
    return fake
