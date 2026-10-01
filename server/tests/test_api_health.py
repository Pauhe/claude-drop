from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(root=tmp_path))) as c:
        yield c


@pytest.fixture
def quiet_client(tmp_path):
    """A client whose background sweep never runs.

    The startup sweeper fires concurrently with the first request and would
    otherwise overwrite the very fields these tests set, so the assertions
    would race rather than test.
    """
    app = create_app(Settings(root=tmp_path))
    app.state.store.sweep_expired = lambda: 0
    with TestClient(app) as c:
        yield c


def test_healthy_service_is_200(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["writable"] is True
    assert body["reason"] is None


def test_unwritable_storage_is_503(client):
    store = client.app.state.store
    store.writable = lambda: (False, "storage is not writable: injected")
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert "writable" in response.json()["reason"]


def test_low_free_space_is_503(tmp_path):
    import shutil
    free = shutil.disk_usage(tmp_path).free
    app = create_app(Settings(root=tmp_path, free_reserve_bytes=free * 2))
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 503
        assert "space" in response.json()["reason"]


def test_being_at_quota_is_503(tmp_path, png_bytes):
    app = create_app(Settings(root=tmp_path, quota_bytes=1))
    with TestClient(app) as client:
        # At quota every upload is rejected, which is not health.
        response = client.get("/healthz")
        assert response.status_code == 200      # nothing stored yet
        client.app.state.store.usage_bytes = lambda: 5
        assert client.get("/healthz").status_code == 503
        assert "quota" in client.get("/healthz").json()["reason"]


def test_a_sweep_that_failed_to_reclaim_is_503(quiet_client):
    client = quiet_client
    store = client.app.state.store
    store._last_sweep = datetime.now(timezone.utc)
    store._last_sweep_failures = 2
    response = client.get("/healthz")
    assert response.status_code == 503
    assert "reclaim" in response.json()["reason"]


def test_a_stalled_sweep_is_503(quiet_client):
    client = quiet_client
    store = client.app.state.store
    store._last_sweep = datetime.now(timezone.utc) - timedelta(
        seconds=store.settings.sweep_interval_seconds * 4)
    response = client.get("/healthz")
    assert response.status_code == 503
    assert "sweep" in response.json()["reason"]


def test_a_sweep_that_has_never_run_is_fine_right_after_startup(client):
    assert client.get("/healthz").status_code == 200


def test_the_response_is_compact_json_so_keyword_monitoring_would_be_fragile(client):
    # Recorded deliberately: an earlier plan configured the Uptime Kuma
    # monitor with the keyword '"status": "ok"'. FastAPI serialises without
    # the space, so that monitor would have called a healthy service dead.
    text = client.get("/healthz").text
    assert '"status":"ok"' in text
    assert '"status": "ok"' not in text


def test_concurrent_health_checks_do_not_fail_each_other(client):
    """A shared probe filename lets one check delete another's file."""
    import threading

    codes, lock = [], threading.Lock()

    def hit():
        code = client.get("/healthz").status_code
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=hit) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes == [200] * 12


def test_the_service_logger_can_actually_emit(tmp_path, caplog):
    """A deletion that is never logged is a deletion nobody can explain."""
    import logging

    from app.main import configure_logging, log

    configure_logging()
    assert log.handlers, "no handler: every log.info would be discarded"
    assert log.getEffectiveLevel() <= logging.INFO
