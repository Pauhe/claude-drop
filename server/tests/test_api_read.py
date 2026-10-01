import logging
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(root=tmp_path))) as c:
        yield c


def upload(client, name, data, ctype):
    return client.post("/api/uploads",
                       files=[("files", (name, data, ctype))]).json()


def test_listing_is_ascending_with_a_snapshot(client, png_bytes):
    upload(client, "a.png", png_bytes, "image/png")
    upload(client, "b.png", png_bytes, "image/png")
    body = client.get("/api/drops").json()
    seqs = [d["seq"] for d in body["drops"]]
    assert seqs == sorted(seqs)
    assert body["snapshot_max_seq"] == seqs[-1]


def test_max_seq_bounds_the_page(client, png_bytes):
    first = upload(client, "a.png", png_bytes, "image/png")
    upload(client, "b.png", png_bytes, "image/png")
    boundary = first["accepted"][0]["seq"]
    body = client.get(f"/api/drops?max_seq={boundary}").json()
    assert [d["seq"] for d in body["drops"]] == [boundary]
    assert body["snapshot_max_seq"] == boundary


def test_order_desc_returns_the_newest(client, png_bytes):
    upload(client, "a.png", png_bytes, "image/png")
    last = upload(client, "b.png", png_bytes, "image/png")
    body = client.get("/api/drops?order=desc&limit=1").json()
    assert body["drops"][0]["id"] == last["accepted"][0]["id"]


def test_batch_filter(client, png_bytes):
    upload(client, "a.png", png_bytes, "image/png")
    wanted = upload(client, "b.png", png_bytes, "image/png")
    body = client.get(f"/api/drops?batch={wanted['batch']}").json()
    assert [d["id"] for d in body["drops"]] == [wanted["accepted"][0]["id"]]


def test_download_is_an_attachment_and_not_sniffable(client, png_bytes):
    drop = upload(client, "a.png", png_bytes, "image/png")["accepted"][0]
    response = client.get(f"/api/drops/{drop['id']}/view_jpg")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-security-policy"] == "default-src 'none'"


def test_uploaded_html_is_never_rendered_under_our_origin(client):
    drop = upload(client, "x.html", b"<script>alert(1)</script>", "text/html")
    response = client.get(f"/api/drops/{drop['accepted'][0]['id']}/orig")
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment")


def test_a_variant_that_does_not_exist_is_404(client, jpeg_bytes):
    drop = upload(client, "a.jpg", jpeg_bytes, "image/jpeg")["accepted"][0]
    assert client.get(f"/api/drops/{drop['id']}/view_png").status_code == 404


def test_an_unknown_variant_name_is_404(client, png_bytes):
    drop = upload(client, "a.png", png_bytes, "image/png")["accepted"][0]
    assert client.get(f"/api/drops/{drop['id']}/nonsense").status_code == 404


# A bare ".." never reaches the handler: httpx collapses /api/drops/.. to
# /api before sending, so asserting on it tests the HTTP client rather than
# this service. Percent-encoded traversal does survive to the route, which is
# the form worth asserting on. Store.delete is covered directly against a raw
# ".." in test_store_publish.py.
@pytest.mark.parametrize("bad", ["notanid", "f" * 31, "F" * 32, "%2e%2e"])
def test_a_malformed_id_is_404_and_changes_nothing(client, png_bytes, bad):
    kept = upload(client, "a.png", png_bytes, "image/png")["accepted"][0]
    assert client.get(f"/api/drops/{bad}/orig").status_code == 404
    assert client.delete(f"/api/drops/{bad}").status_code == 404
    assert len(client.get("/api/drops").json()["drops"]) == 1
    assert client.get(f"/api/drops/{kept['id']}/orig").status_code == 200


def test_an_encoded_slash_is_refused_and_changes_nothing(client, png_bytes):
    """%2F decodes to a separator, so the id route does not match at all and
    the request falls through to the static mount. The status code is a
    routing detail worth no assertion; what matters is that it is refused and
    the data is untouched."""
    kept = upload(client, "a.png", png_bytes, "image/png")["accepted"][0]
    bad = "%2E%2E%2F%2E%2E"
    assert client.get(f"/api/drops/{bad}/orig").status_code >= 400
    assert client.delete(f"/api/drops/{bad}").status_code >= 400
    assert len(client.get("/api/drops").json()["drops"]) == 1
    assert client.get(f"/api/drops/{kept['id']}/orig").status_code == 200


def test_delete_removes_the_drop(client, png_bytes):
    drop = upload(client, "a.png", png_bytes, "image/png")["accepted"][0]
    assert client.delete(f"/api/drops/{drop['id']}").status_code == 204
    assert client.get("/api/drops").json()["drops"] == []


def test_deleting_twice_is_404_the_second_time(client, png_bytes):
    drop = upload(client, "a.png", png_bytes, "image/png")["accepted"][0]
    client.delete(f"/api/drops/{drop['id']}")
    assert client.delete(f"/api/drops/{drop['id']}").status_code == 404


def test_download_name_carries_a_usable_extension(client, png_bytes):
    """A download called 'a1b2c3-orig' with no extension is one the OS
    cannot open — which reads to a person as a broken button."""
    drop = upload(client, "Bildschirmfoto 2026.png", png_bytes,
                  "image/png")["accepted"][0]
    for variant, expected in (("orig", ".png"), ("view_jpg", ".jpg"),
                              ("view_png", ".png")):
        response = client.get(f"/api/drops/{drop['id']}/{variant}")
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment")
        assert expected in disposition, (variant, disposition)
        assert "Bildschirmfoto" in disposition


def test_download_name_is_sanitised(client, png_bytes):
    drop = upload(client, "../../etc/pa ss wd.png", png_bytes,
                  "image/png")["accepted"][0]
    disposition = client.get(f"/api/drops/{drop['id']}/orig"
                             ).headers["content-disposition"]
    assert "/" not in disposition.split("filename=")[1]
    assert " " not in disposition.split("filename=")[1]


class _Capture(logging.Handler):
    """Attach to the real logger.

    configure_logging() sets propagate=False on 'claude_drop' so a later
    root configuration cannot double-log, which also means caplog's
    root handler never sees these records. Capturing at the source tests
    what actually ships.
    """

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def __enter__(self):
        logger = logging.getLogger("claude_drop")
        logger.setLevel(logging.INFO)
        logger.addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger("claude_drop").removeHandler(self)

    def messages(self, min_level=logging.INFO):
        return [r.getMessage() for r in self.records if r.levelno >= min_level]


def test_a_deletion_is_logged_with_what_it_destroyed(client, png_bytes):
    """Twenty-nine drops were deleted by hand in one session and produced no
    log line at all, which is indistinguishable from data vanishing."""
    drop = upload(client, "wichtig.png", png_bytes, "image/png")["accepted"][0]
    with _Capture() as cap:
        assert client.delete(f"/api/drops/{drop['id']}").status_code == 204
    warnings = cap.messages(logging.WARNING)
    assert warnings, "a deletion must be visible in the log"
    assert drop["id"][:12] in warnings[0]
    assert "wichtig.png" in warnings[0]


def test_an_upload_is_logged(client, png_bytes):
    with _Capture() as cap:
        upload(client, "neu.png", png_bytes, "image/png")
    assert any("upload:" in m and "neu.png" in m for m in cap.messages())


def test_a_deletion_of_an_unknown_drop_is_logged_too(client):
    with _Capture() as cap:
        client.delete("/api/drops/" + "f" * 32)
    assert any("not found" in m for m in cap.messages())
