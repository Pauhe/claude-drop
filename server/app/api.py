"""HTTP surface. No authentication by design — Tailscale is the boundary and
the service binds no host port. See docs/design.md."""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query, Request, Response
from fastapi import HTTPException as FastAPIHTTPException
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile
# Starlette's own HTTPException, not FastAPI's: FastAPI subclasses it, so
# catching the subclass would miss what request.form() actually raises.
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from app import images
from app.store import Drop, QuotaExceeded

log = logging.getLogger("claude_drop.api")

router = APIRouter()

# /app inside the image, where the Dockerfile places install.sh and assets/.
# In a source checkout this resolves to <repo>/server, which holds none of
# them — so a checkout falls back to the repository root.
_PACKAGED = Path(__file__).parent.parent
ASSETS = _PACKAGED if (_PACKAGED / "install.sh").exists() else _PACKAGED.parent

DOWNLOAD_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'",
}


def _json(drop: Drop) -> dict:
    return {
        "id": drop.id, "seq": drop.seq, "batch": drop.batch,
        "created_at": drop.created_at.isoformat(), "filename": drop.filename,
        "media_type": drop.media_type, "size": drop.size, "width": drop.width,
        "height": drop.height, "kind": drop.kind, "variants": drop.variants,
    }


def _spooled_size(item: StarletteUploadFile) -> int:
    size = getattr(item, "size", None)
    if size is not None:
        return size
    item.file.seek(0, os.SEEK_END)
    size = item.file.tell()
    item.file.seek(0)
    return size


@router.post("/api/uploads")
async def upload(request: Request) -> JSONResponse:
    """Accept up to max_files parts, each at most max_file_bytes.

    The part count is enforced by the parser. The per-file size is enforced
    after Starlette has spooled the part to a temporary file and before a
    single byte is read into memory or handed to a decoder — so an oversized
    upload costs temporary disk, not RAM.

    Starlette 0.41.3, which FastAPI 0.115.6 pins, offers max_files and
    max_fields and nothing else: there is no max_part_size to lean on. See
    C4 for why this layering is the bound rather than a byte-counting parser.
    """
    settings = request.app.state.settings
    store = request.app.state.store
    slots = request.app.state.decode_slots

    try:
        form = await request.form(max_files=settings.max_files_per_request)
    except MultiPartException as exc:
        return JSONResponse({"error": str(exc)}, status_code=413)
    except StarletteHTTPException as exc:
        # Starlette converts a parser violation into HTTPException(400) once a
        # request is running inside an app, so the MultiPartException above
        # never arrives here. Only form() runs in this try, so narrowing 400
        # to "too large" is precise rather than a catch-all.
        if exc.status_code == 400:
            return JSONResponse({"error": exc.detail}, status_code=413)
        raise

    try:
        uploads = [value for key, value in form.multi_items()
                   if key == "files" and isinstance(value, StarletteUploadFile)]
        if not uploads:
            return JSONResponse({"error": "no files"}, status_code=400)

        batch = await run_in_threadpool(store.new_batch_id)
        accepted, rejected = [], []
        for item in uploads:
            size = _spooled_size(item)
            if size > settings.max_file_bytes:
                rejected.append({
                    "filename": item.filename or "",
                    "error": f"file is {size} bytes, over the "
                             f"{settings.max_file_bytes} byte limit",
                })
                continue

            try:
                def work(upload: StarletteUploadFile = item,
                         name: str = item.filename or "unnamed") -> Drop:
                    # The body is read only once a slot is held, and the slot
                    # covers BOTH the decode and the publish: that is what
                    # bounds total memory. Waiting requests keep their upload
                    # spooled on disk, not in RAM.
                    with slots:
                        upload.file.seek(0)
                        payload = upload.file.read()
                        return store.publish(batch=batch, filename=name,
                                             original=payload,
                                             normalised=images.normalise(payload))

                drop = await run_in_threadpool(work)
            except QuotaExceeded as exc:
                rejected.append({"filename": item.filename or "", "error": str(exc)})
                continue
            log.info("upload: %s seq=%d batch=%s kind=%s %s (%d bytes)",
                     drop.id[:12], drop.seq, batch, drop.kind,
                     drop.filename, drop.size)
            accepted.append({
                "id": drop.id, "seq": drop.seq, "filename": drop.filename,
                "kind": drop.kind, "variants": drop.variants,
            })
        return JSONResponse({"batch": batch, "accepted": accepted,
                             "rejected": rejected})
    finally:
        # An explicitly parsed form owns temporary files; closing releases them.
        await form.close()


@router.get("/api/drops")
def list_drops(
    request: Request,
    after_seq: int | None = None,
    max_seq: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    batch: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    order: str = Query("asc", pattern="^(asc|desc)$"),
) -> dict:
    page = request.app.state.store.list(
        after_seq=after_seq, max_seq=max_seq, since=since, until=until,
        batch=batch, limit=limit, order=order,
    )
    return {"drops": [_json(d) for d in page.drops],
            "snapshot_max_seq": page.snapshot_max_seq}


@router.get("/api/drops/{drop_id}/{variant}")
def download(request: Request, drop_id: str, variant: str) -> FileResponse:
    store = request.app.state.store
    drop = store.get(drop_id)               # returns None for a malformed id
    if drop is None or variant not in drop.variants:
        raise FastAPIHTTPException(404, "no such drop or variant")
    path = store.blob_path(drop_id, variant)
    if not path.exists():
        raise FastAPIHTTPException(404, "blob is gone")
    # Always octet-stream and always an attachment: serving an uploaded .html
    # or .svg as itself would be stored XSS on our own origin. The NAME may
    # still be friendly — a download called "a1b2c3-orig" with no extension
    # is one the OS cannot open, which reads as a broken button.
    return FileResponse(path, media_type="application/octet-stream",
                        filename=download_name(drop, variant),
                        headers=DOWNLOAD_HEADERS)


def download_name(drop: Drop, variant: str) -> str:
    """A filename a person can use: the uploaded stem plus a real extension.

    The uploaded name is untrusted, so it is reduced to safe characters and
    never used as a path — this is only the Content-Disposition label.
    """
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(drop.filename).stem).strip("-.")
    stem = (stem or "drop")[:60]
    suffix = {"view_jpg": ".jpg", "full_jpg": ".jpg", "view_png": ".png"}.get(variant)
    if suffix is None:          # orig: follow the detected type, not the name
        suffix = mimetypes.guess_extension(drop.media_type) or ""
        if drop.media_type == "image/heic":
            suffix = ".heic"    # not in every mimetypes table
        if not suffix:
            suffix = re.sub(r"[^A-Za-z0-9.]+", "", Path(drop.filename).suffix)[:10]
    marker = "" if variant in ("orig", "view_jpg") else f"-{variant}"
    return f"{stem}{marker}{suffix or '.bin'}"


@router.delete("/api/drops/{drop_id}", status_code=204)
def delete(request: Request, drop_id: str) -> Response:
    store = request.app.state.store
    doomed = store.get(drop_id)     # read before destroying, to name it
    if not store.delete(drop_id):
        log.info("delete: %s not found", str(drop_id)[:12])
        raise FastAPIHTTPException(404, "no such drop")
    client = request.client.host if request.client else "?"
    if doomed is not None:
        log.warning("delete: %s seq=%d batch=%s %s (%d bytes) by %s",
                    doomed.id[:12], doomed.seq, doomed.batch, doomed.filename,
                    doomed.size, client)
    else:
        log.warning("delete: %s (expired, no metadata) by %s",
                    str(drop_id)[:12], client)
    return Response(status_code=204)


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    """200 only when the service can actually accept a drop.

    Liveness alone is not health here: a process that is up but cannot write
    to its volume, is at quota, or whose retention sweep died would fail
    silently otherwise — the way a dead timer can go unnoticed for months.
    """
    store = request.app.state.store
    settings = store.settings
    writable, write_reason = store.writable()
    free = shutil.disk_usage(settings.root).free
    usage = store.usage_bytes()
    last_sweep = store.last_sweep

    reason = None
    if not writable:
        reason = write_reason
    elif free < settings.free_reserve_bytes:
        reason = "free space below the reserve"
    elif usage >= settings.quota_bytes:
        reason = "storage quota reached; uploads are being rejected"
    elif store.last_sweep_failures:
        reason = f"last sweep failed to reclaim {store.last_sweep_failures} drops"
    elif last_sweep is None:
        if time.monotonic() - request.app.state.started_at > settings.sweep_interval_seconds:
            reason = "retention sweep has never run"
    else:
        age = (datetime.now(timezone.utc) - last_sweep).total_seconds()
        if age > settings.sweep_interval_seconds * 3:
            reason = f"retention sweep stalled for {int(age)}s"

    body = {
        "status": "ok" if reason is None else "degraded",
        "writable": writable,
        "usage_bytes": usage,
        "free_bytes": free,
        "last_sweep": last_sweep.isoformat() if last_sweep else None,
        "drops": store.count(),
        "reason": reason,
    }
    return JSONResponse(body, status_code=200 if reason is None else 503)


@router.get("/install.sh")
def installer() -> FileResponse:
    """No secret in it — the service has no credentials."""
    return FileResponse(ASSETS / "install.sh", media_type="text/x-shellscript")


@router.get("/cli/drop")
def cli_asset() -> FileResponse:
    name = "drop" if (ASSETS / "assets" / "drop").exists() else None
    path = (ASSETS / "assets" / "drop") if name else (ASSETS / "cli" / "drop")
    return FileResponse(path, media_type="text/x-python")


@router.get("/skill/SKILL.md")
def skill_asset() -> FileResponse:
    packaged = ASSETS / "assets" / "SKILL.md"
    path = packaged if packaged.exists() else (ASSETS / "skill" / "SKILL.md")
    return FileResponse(path, media_type="text/markdown")
