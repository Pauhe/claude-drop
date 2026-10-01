import threading

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(root=tmp_path))) as c:
        yield c


def upload(client, files):
    return client.post("/api/uploads", files=files)


def stored(client):
    """Read through the store: the listing route arrives in Task 7, and a
    task's tests must pass at its own commit."""
    return client.app.state.store.list(limit=500).drops


def test_upload_returns_a_batch_and_receipts(client, png_bytes):
    body = upload(client, [("files", ("shot.png", png_bytes, "image/png"))]).json()
    assert len(body["batch"]) == 8
    assert len(body["accepted"]) == 1
    assert body["rejected"] == []
    assert body["accepted"][0]["kind"] == "image"
    assert "view_png" in body["accepted"][0]["variants"]


def test_one_request_shares_one_batch(client, png_bytes, jpeg_bytes):
    body = upload(client, [
        ("files", ("a.png", png_bytes, "image/png")),
        ("files", ("b.jpg", jpeg_bytes, "image/jpeg")),
    ]).json()
    assert len(body["accepted"]) == 2
    assert {d.batch for d in stored(client)} == {body["batch"]}


def test_an_oversized_file_is_rejected_before_it_is_decoded(tmp_path, png_bytes):
    app = create_app(Settings(root=tmp_path, max_file_bytes=len(png_bytes) - 1))
    with TestClient(app) as client:
        body = upload(client,
                      [("files", ("shot.png", png_bytes, "image/png"))]).json()
        assert body["accepted"] == []
        assert "byte limit" in body["rejected"][0]["error"]
        assert stored(client) == []


def test_too_many_files_is_refused(tmp_path, png_bytes):
    app = create_app(Settings(root=tmp_path, max_files_per_request=2))
    with TestClient(app) as client:
        response = upload(client, [
            ("files", (f"{i}.png", png_bytes, "image/png")) for i in range(3)
        ])
        assert response.status_code == 413


def test_a_non_file_field_named_files_is_ignored(client, png_bytes):
    response = client.post(
        "/api/uploads",
        data={"files": "not a file"},
        files=[("files", ("a.png", png_bytes, "image/png"))],
    )
    assert response.status_code == 200
    assert len(response.json()["accepted"]) == 1


def test_a_request_with_no_files_is_400(client):
    response = client.post("/api/uploads", data={"other": "x"})
    assert response.status_code == 400


def test_a_non_image_is_stored_raw(client):
    body = upload(client, [("files", ("notes.txt", b"hello", "text/plain"))]).json()
    assert body["accepted"][0]["kind"] == "raw"
    assert body["accepted"][0]["variants"] == ["orig"]


def test_a_quota_rejection_is_a_receipt_not_a_failed_request(tmp_path, png_bytes):
    app = create_app(Settings(root=tmp_path, quota_bytes=1))
    with TestClient(app) as client:
        response = upload(client, [("files", ("a.png", png_bytes, "image/png"))])
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == []
        assert "quota" in body["rejected"][0]["error"]


def test_a_corrupt_image_is_accepted_and_marked_failed(client, png_bytes):
    body = upload(client, [("files", ("x.png", png_bytes[:40], "image/png"))]).json()
    assert body["accepted"][0]["kind"] == "failed"


def test_batch_ids_differ_between_requests(client, png_bytes):
    first = upload(client, [("files", ("a.png", png_bytes, "image/png"))]).json()
    second = upload(client, [("files", ("b.png", png_bytes, "image/png"))]).json()
    assert first["batch"] != second["batch"]


def test_a_retried_upload_creates_a_second_batch(client, png_bytes):
    """Documented behaviour, not a bug: a lost response followed by a retry
    produces duplicate drops in a new batch, which the user deletes in the UI.
    Deduplicating would need client-generated idempotency keys."""
    first = upload(client, [("files", ("a.png", png_bytes, "image/png"))]).json()
    second = upload(client, [("files", ("a.png", png_bytes, "image/png"))]).json()
    assert first["batch"] != second["batch"]
    assert len(stored(client)) == 2


def test_decoding_is_bounded_by_the_semaphore(tmp_path, png_bytes):
    """The bound must be the semaphore, not the arrival rate."""
    app = create_app(Settings(root=tmp_path, max_concurrent_decodes=2))
    peak = 0
    current = 0
    lock = threading.Lock()
    real = app.state.decode_slots

    class Counting:
        def __enter__(self):
            nonlocal peak, current
            real.acquire()
            with lock:
                current += 1
                peak = max(peak, current)
            return self

        def __exit__(self, *exc):
            nonlocal current
            with lock:
                current -= 1
            real.release()

    app.state.decode_slots = Counting()
    with TestClient(app) as client:
        threads = [threading.Thread(target=lambda: client.post(
            "/api/uploads",
            files=[("files", ("a.png", png_bytes, "image/png"))]))
            for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert peak <= 2
