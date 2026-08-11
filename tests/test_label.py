from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import k12ta.label.app as label_app

STEM_MARK = 'name="stem" value="'


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    fixtures = tmp_path / "fixtures"
    pages = fixtures / "pages"
    pages.mkdir(parents=True)
    monkeypatch.setattr(label_app, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(label_app, "PAGES_DIR", pages)
    monkeypatch.setattr(label_app, "CACHE_DIR", fixtures / ".cache")
    return TestClient(label_app.app)


def _touch(pages: Path, name: str) -> None:
    (pages / name).touch()


def _current_stem(html: str) -> str:
    return html.split(STEM_MARK)[1].split('"')[0]


def _minimal_save(stem: str, **overrides: str) -> dict[str, str]:
    data = {
        "stem": stem,
        "action": "save",
        "row_count": "10",
        "source_id": "summer_bridge",
        "subject": "math",
        "capture_quality": "good",
        "capture_device": "ipad-air-m1",
        "capture_method": "camera-roll",
    }
    data.update(overrides)
    return data


def test_get_label_shows_first_unlabelled_image_with_no_prefill(client: TestClient) -> None:
    _touch(label_app.PAGES_DIR, "b.jpg")
    _touch(label_app.PAGES_DIR, "a.jpg")

    r = client.get("/label")

    assert r.status_code == 200
    assert _current_stem(r.text) == "a"
    assert "0 / 2 labelled" in r.text
    assert 'name="capture_device" value=""' in r.text


def test_save_with_no_rows_filled_is_rejected_and_writes_nothing(client: TestClient) -> None:
    _touch(label_app.PAGES_DIR, "a.jpg")

    r = client.post("/label", data=_minimal_save("a"))

    assert r.status_code == 200
    assert "No problems entered" in r.text
    assert not (label_app.FIXTURES_DIR / "a.json").exists()


def test_add_rows_preserves_typed_values_and_grows_row_count(client: TestClient) -> None:
    _touch(label_app.PAGES_DIR, "a.jpg")
    data = {
        "stem": "a",
        "action": "add_rows",
        "row_count": "10",
        "problem_id_0": "keep-me",
    }

    r = client.post("/label", data=data)

    assert 'name="row_count" value="15"' in r.text
    assert "problem_id_14" in r.text
    assert 'value="keep-me"' in r.text
    assert not (label_app.FIXTURES_DIR / "a.json").exists()


def test_skip_writes_zero_item_label_and_advances(client: TestClient) -> None:
    _touch(label_app.PAGES_DIR, "a.jpg")
    _touch(label_app.PAGES_DIR, "b.jpg")

    r = client.post(
        "/label", data=_minimal_save("a", action="skip"), follow_redirects=False
    )

    assert r.status_code == 303
    saved = json.loads((label_app.FIXTURES_DIR / "a.json").read_text())
    assert saved["items"] == []
    assert saved["capture_device"] == "ipad-air-m1"

    next_page = client.get("/label")
    assert _current_stem(next_page.text) == "b"


def test_prefill_carries_source_subject_method_but_not_device_or_quality(
    client: TestClient,
) -> None:
    _touch(label_app.PAGES_DIR, "a.jpg")
    _touch(label_app.PAGES_DIR, "b.jpg")
    client.post(
        "/label",
        data=_minimal_save(
            "a", capture_device="Pixel 9a", capture_quality="poor", action="skip"
        ),
    )

    r = client.get("/label")

    assert 'value="summer_bridge"' in r.text  # source_id carried forward
    assert 'name="capture_device" value=""' in r.text  # never carried forward
    assert 'name="capture_quality" value=""' in r.text  # never carried forward
    assert 'selected>camera-roll' in r.text  # capture_method carried forward


def test_save_writes_original_image_path_and_normalised_device(client: TestClient) -> None:
    _touch(label_app.PAGES_DIR, "a.jpg")

    r = client.post(
        "/label",
        data=_minimal_save(
            "a",
            capture_device="  Pixel   9a  ",
            problem_id_0="1",
            prompt_text_0="What is 2+2?",
            student_answer_raw_0="4",
            correct_answer_0="4",
            human_legible_0="1",
        ),
        follow_redirects=False,
    )

    assert r.status_code == 303
    saved = json.loads((label_app.FIXTURES_DIR / "a.json").read_text())
    assert saved["image"] == "pages/a.jpg"
    assert saved["capture_device"] == "pixel-9a"
    assert saved["items"] == [
        {
            "problem_id": "1",
            "prompt_text": "What is 2+2?",
            "student_answer_raw": "4",
            "human_legible": True,
            "correct_answer": "4",
        }
    ]


def test_blank_rows_are_dropped_but_filled_rows_are_kept(client: TestClient) -> None:
    _touch(label_app.PAGES_DIR, "a.jpg")

    client.post(
        "/label",
        data=_minimal_save(
            "a", problem_id_0="1", student_answer_raw_0="4", problem_id_3="4"
        ),
    )

    saved = json.loads((label_app.FIXTURES_DIR / "a.json").read_text())
    assert [item["problem_id"] for item in saved["items"]] == ["1", "4"]


def test_all_pages_labelled_shows_done_page(client: TestClient) -> None:
    _touch(label_app.PAGES_DIR, "a.jpg")

    client.post("/label", data=_minimal_save("a", action="skip"))
    r = client.get("/label")

    assert "All 1 pages labelled" in r.text


def test_display_copy_skips_conversion_for_non_heic(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fake_run(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(label_app.subprocess, "run", fake_run)
    image = label_app.PAGES_DIR / "a.jpg"
    image.touch()

    result = label_app._display_copy(image)

    assert result == image
    assert called is False


def test_display_copy_converts_heic_and_caches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def fake_run(cmd: list[str], **kwargs: object) -> None:
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"fake-jpeg")

    monkeypatch.setattr(label_app.subprocess, "run", fake_run)
    image = label_app.PAGES_DIR / "a.HEIC"
    image.touch()

    first = label_app._display_copy(image)
    second = label_app._display_copy(image)

    assert first == label_app.CACHE_DIR / "a.jpg"
    assert first.read_bytes() == b"fake-jpeg"
    assert len(calls) == 1  # second call reused the cache, sips ran only once
    assert calls[0][0] == "sips"
    assert second == first


def test_pages_lists_every_image_with_labelled_status(client: TestClient) -> None:
    _touch(label_app.PAGES_DIR, "a.jpg")
    _touch(label_app.PAGES_DIR, "b.jpg")
    client.post("/label", data=_minimal_save("a", action="skip"))

    r = client.get("/pages")

    assert r.status_code == 200
    assert "1 / 2 labelled" in r.text
    assert 'href="/label/a"' in r.text
    assert 'href="/label/b"' in r.text


def test_revisiting_an_already_labelled_page_prefills_its_own_saved_values(
    client: TestClient,
) -> None:
    _touch(label_app.PAGES_DIR, "a.jpg")
    client.post(
        "/label",
        data=_minimal_save(
            "a",
            capture_device="Pixel 9a",
            capture_quality="poor",
            problem_id_0="7",
            prompt_text_0="What is 6+6?",
            student_answer_raw_0="12",
            correct_answer_0="12",
            human_legible_0="1",
        ),
    )

    r = client.get("/label/a")

    assert r.status_code == 200
    # Editing shows the page's own true values, not the partial "carried forward" set.
    assert 'name="capture_device" value="pixel-9a"' in r.text
    assert 'name="capture_quality" value="poor"' in r.text
    assert 'value="7"' in r.text
    assert 'value="What is 6+6?"' in r.text
    assert "carried forward" not in r.text


def test_resaving_an_edited_page_overwrites_it_and_returns_to_the_page_list(
    client: TestClient,
) -> None:
    _touch(label_app.PAGES_DIR, "a.jpg")
    client.post(
        "/label",
        data=_minimal_save("a", problem_id_0="1", student_answer_raw_0="4"),
    )

    r = client.post(
        "/label",
        data=_minimal_save("a", problem_id_0="1", student_answer_raw_0="5"),
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"] == "/pages"
    saved = json.loads((label_app.FIXTURES_DIR / "a.json").read_text())
    assert saved["items"][0]["student_answer_raw"] == "5"


def test_saving_a_brand_new_page_still_returns_to_next_unlabelled(
    client: TestClient,
) -> None:
    _touch(label_app.PAGES_DIR, "a.jpg")

    r = client.post(
        "/label",
        data=_minimal_save("a", problem_id_0="1", student_answer_raw_0="4"),
        follow_redirects=False,
    )

    assert r.headers["location"] == "/label"
