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


def test_both_apps_render_the_same_shared_lightbox_partial() -> None:
    """M9b (docs/ROADMAP.md): the lightbox's HTML/JS, not just its CSS, is
    now one physical file too (k12ta.design/_lightbox.html), reached via
    each app's Jinja2Templates search path rather than a copy in either
    app's own templates/ directory. Rendered directly (no HTTP, no database)
    since this partial takes no template variables."""
    web_html = web_app.templates.get_template("_lightbox.html").render()
    keys_html = keys_app.templates.get_template("_lightbox.html").render()

    assert web_html == keys_html
    assert 'id="lightbox-overlay"' in web_html
    assert "data-lightbox" in web_html


def test_neither_app_keeps_its_own_copy_of_the_lightbox_partial() -> None:
    """The actual point of M9b's half of this: nothing left to drift."""
    from pathlib import Path

    web_templates_dir = Path(web_app.__file__).parent / "templates"
    keys_templates_dir = Path(keys_app.__file__).parent / "templates"

    assert not (web_templates_dir / "_lightbox.html").exists()
    assert not (keys_templates_dir / "_lightbox.html").exists()
