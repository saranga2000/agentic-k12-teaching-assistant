"""M9a (docs/ROADMAP.md): one shared design-system stylesheet, served by both
apps from the same physical file (src/k12ta/design/tokens.css) -- not two
copies that could drift. Static file serving needs no database or model
dependency overrides, so these use a bare TestClient rather than the heavier
fixtures test_web_capture.py/test_keys_app.py set up for routes that do.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import k12ta.keys.app as keys_app
import k12ta.web.app as web_app


def test_web_app_serves_the_shared_design_tokens() -> None:
    response = TestClient(web_app.app).get("/static/tokens.css")

    assert response.status_code == 200
    assert "--affirm" in response.text
    assert "--radius-lg" in response.text


def test_keys_app_serves_the_same_shared_design_tokens() -> None:
    response = TestClient(keys_app.app).get("/static/tokens.css")

    assert response.status_code == 200
    assert "--affirm" in response.text
    assert "--radius-lg" in response.text


def test_both_apps_serve_byte_identical_tokens_css() -> None:
    """The actual point of M9a: one file, not two copies that could drift."""
    web_css = TestClient(web_app.app).get("/static/tokens.css").text
    keys_css = TestClient(keys_app.app).get("/static/tokens.css").text

    assert web_css == keys_css
