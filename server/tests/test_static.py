import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def page(tmp_path) -> str:
    with TestClient(create_app(Settings(root=tmp_path))) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        return response.text


def test_offers_camera_capture(page):
    assert 'capture="environment"' in page


def test_handles_clipboard_paste(page):
    assert re.search(r'addEventListener\(\s*"paste"', page)


def test_requests_the_newest_drops_not_the_oldest(page):
    # The API is ascending by default; a plain limit=40 returns the oldest 40.
    assert "order=desc" in page


def test_receipts_live_outside_the_list_that_gets_rebuilt(page):
    assert 'id="receipts"' in page
    assert 'id="recent"' in page


def test_offers_the_original_for_download(page):
    assert "/orig" in page


def test_produces_a_batch_sentence(page):
    assert "drop batch" in page


def test_mounting_static_did_not_shadow_the_api(tmp_path, png_bytes):
    with TestClient(create_app(Settings(root=tmp_path))) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/drops").status_code == 200
        posted = client.post("/api/uploads",
                             files=[("files", ("a.png", png_bytes, "image/png"))])
        assert posted.status_code == 200
