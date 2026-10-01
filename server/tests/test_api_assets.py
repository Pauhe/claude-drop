import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.api as api
    # In the image these live under /app; in a checkout, at the repo root.
    root = tmp_path / "app_root"
    (root / "assets").mkdir(parents=True)
    (root / "install.sh").write_text("#!/bin/sh\necho hi\n")
    (root / "assets" / "drop").write_text("#!/usr/bin/env python3\n")
    (root / "assets" / "SKILL.md").write_text("# claude-drop\n")
    monkeypatch.setattr(api, "ASSETS", root)
    with TestClient(create_app(Settings(root=tmp_path / "data"))) as c:
        yield c


def test_installer_is_served(client):
    response = client.get("/install.sh")
    assert response.status_code == 200
    assert response.text.startswith("#!/bin/sh")


def test_cli_is_served(client):
    assert client.get("/cli/drop").status_code == 200


def test_skill_is_served(client):
    assert client.get("/skill/SKILL.md").status_code == 200
