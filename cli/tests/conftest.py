import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff" + b"\x00" * 64


class Fake:
    """Stand-in for the service that enforces the same filters the real one
    does — a fake that ignores `since` hides exactly the bugs worth finding."""

    def __init__(self):
        self.drops: list[dict] = []
        self.fail_downloads = False
        self.truncate_downloads = False
        self.paths: list[str] = []
        self.url = ""

    def add(self, *, batch="b1", filename="shot.png", kind="image",
            variants=("orig", "view_png", "view_jpg"), minutes_ago=0,
            media_type="image/png"):
        seq = len(self.drops) + 1
        created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        drop = {"id": f"{seq:032x}", "seq": seq, "batch": batch,
                "created_at": created.isoformat(), "filename": filename,
                "media_type": media_type, "size": 64, "width": 10, "height": 10,
                "kind": kind, "variants": list(variants)}
        self.drops.append(drop)
        return drop

    def select(self, query) -> tuple[list[dict], int]:
        rows = list(self.drops)
        if "after_seq" in query:
            rows = [d for d in rows if d["seq"] > int(query["after_seq"][0])]
        if "batch" in query:
            rows = [d for d in rows if d["batch"] == query["batch"][0]]
        if "since" in query:
            since = datetime.fromisoformat(query["since"][0])
            rows = [d for d in rows
                    if datetime.fromisoformat(d["created_at"]) >= since]
        boundary = (int(query["max_seq"][0]) if "max_seq" in query
                    else max([d["seq"] for d in rows], default=0))
        rows = [d for d in rows if d["seq"] <= boundary]
        if query.get("order", ["asc"])[0] == "desc":
            rows.reverse()
        return rows[: int(query.get("limit", ["100"])[0])], boundary


@pytest.fixture
def service():
    state = Fake()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state.paths.append(self.path)
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/drops/"):
                if state.fail_downloads:
                    self.send_error(500)
                    return
                variant = parsed.path.rsplit("/", 1)[-1]
                payload = JPG if variant.endswith("jpg") else PNG
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                # Content-Length always describes the whole body; truncation
                # sends fewer bytes, which is what a cut connection looks like.
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload[:8] if state.truncate_downloads
                                 else payload)
                return
            rows, boundary = state.select(parse_qs(parsed.query))
            body = json.dumps({"drops": rows,
                               "snapshot_max_seq": boundary}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
