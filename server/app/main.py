"""App factory, startup recovery and the periodic retention sweep."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException

from app.api import router
from app.config import Settings, settings_from_env
from app.store import Store

log = logging.getLogger("claude_drop")
STATIC = Path(__file__).parent / "static"


def configure_logging() -> None:
    """Make this service's own messages actually reach the container log.

    Without this the logger has no handler and an effective level of
    WARNING, so every log.info — including "retention sweep removed N
    drops" — is discarded. Deletions are the events most worth being able
    to read back afterwards, and they were the ones going missing.
    """
    if log.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    staging, orphans = await run_in_threadpool(app.state.store.recover)
    if staging or orphans:
        log.warning("recovered %d staging dirs, %d orphan blobs", staging, orphans)
    app.state.sweeper = asyncio.create_task(_sweep_forever(app))
    try:
        yield
    finally:
        app.state.sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await app.state.sweeper
        app.state.store.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    settings = settings or settings_from_env()
    app = FastAPI(title="claude-drop", docs_url=None, redoc_url=None,
                  lifespan=_lifespan)
    app.state.settings = settings
    app.state.store = Store(settings)
    app.state.decode_slots = threading.Semaphore(settings.max_concurrent_decodes)
    app.state.started_at = time.monotonic()
    app.include_router(router)
    # Mounted after the router so /api/... and /healthz keep their handlers.
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")

    @app.exception_handler(MultiPartException)
    async def _too_large(request: Request, exc: MultiPartException) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=413)

    return app


async def _sweep_forever(app: FastAPI) -> None:
    store = app.state.store
    while True:
        try:
            # Off the event loop: the sweep does synchronous filesystem work
            # and would otherwise stall /healthz while it runs.
            deleted = await run_in_threadpool(store.sweep_expired)
            log.info("retention sweep removed %d drops (%d kept, %d failed)",
                     deleted, store.count(), store.last_sweep_failures)
        except Exception:       # noqa: BLE001 — a failed sweep must not end the loop
            log.exception("retention sweep failed")
        await asyncio.sleep(store.settings.sweep_interval_seconds)


def app() -> FastAPI:
    """uvicorn factory entrypoint: uvicorn app.main:app --factory"""
    return create_app()
