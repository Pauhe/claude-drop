"""Blob storage with a SQLite index.

Three invariants live here and nowhere else:

1. A drop becomes visible atomically. Blobs are staged on the same
   filesystem, fsynced, renamed into place, and only then inserted.
2. No directory is removed for an id that has no row. An earlier draft
   called rmtree unconditionally, so an id of ".." pointed at the data root
   and ignore_errors=True hid the result.
3. Each thread gets its own connection. Sharing one gives concurrent
   callers the same transaction, so one request's commit can finalise
   another's half-finished work.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings
from app.images import Normalised

log = logging.getLogger("claude_drop.store")

ID_PATTERN = re.compile(r"[0-9a-f]{32}")

VARIANT_FILES = {
    "orig": "orig",
    "view_jpg": "view_jpg.jpg",
    "view_png": "view_png.png",
    "full_jpg": "full_jpg.jpg",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS drops (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT NOT NULL UNIQUE,
    batch      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    filename   TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size       INTEGER NOT NULL,
    width      INTEGER,
    height     INTEGER,
    kind       TEXT NOT NULL,
    variants   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS drops_batch ON drops(batch);
CREATE INDEX IF NOT EXISTS drops_created ON drops(created_at);
CREATE TABLE IF NOT EXISTS batches (
    batch      TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""


class QuotaExceeded(Exception):
    """Raised instead of evicting someone else's drops to make room."""


@dataclass(frozen=True)
class Drop:
    id: str
    seq: int
    batch: str
    created_at: datetime
    filename: str
    media_type: str
    size: int
    width: int | None
    height: int | None
    kind: str
    variants: list[str]


@dataclass(frozen=True)
class Page:
    drops: list[Drop]
    snapshot_max_seq: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_id(drop_id: str) -> str:
    # fullmatch, not match: `$` also matches before a trailing newline, so
    # "aaaa...aaa\n" would pass — exactly the input this check exists to stop.
    if not isinstance(drop_id, str) or not ID_PATTERN.fullmatch(drop_id):
        raise ValueError(f"not a drop id: {drop_id!r}")
    return drop_id


class Store:
    def __init__(self, settings: Settings, now: Callable[[], datetime] = _utcnow):
        self.settings = settings
        self._now = now
        self._local = threading.local()
        self._publish_lock = threading.Lock()
        self._last_sweep: datetime | None = None
        self._last_sweep_failures = 0
        self.blobs = settings.root / "blobs"
        self.staging = settings.root / "staging"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            conn.executescript(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.settings.root / "db.sqlite3", timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------------------------------------------------------------- writes

    def new_batch_id(self) -> str:
        """Reserve a batch id atomically.

        Querying drops for a free candidate and then using it races: two
        requests can both find the same id unused before either publishes a
        row. A UNIQUE insert makes the reservation the check.
        """
        conn = self._db()
        while True:
            candidate = secrets.token_hex(4)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO batches (batch, created_at) VALUES (?, ?)",
                        (candidate, self._now().isoformat()),
                    )
                return candidate
            except sqlite3.IntegrityError:
                continue

    def publish(self, *, batch: str, filename: str, original: bytes,
                normalised: Normalised) -> Drop:
        incoming = len(original) + sum(len(v.data) for v in normalised.variants)
        drop_id = secrets.token_hex(16)

        # The capacity check and the write are one critical section: two
        # uploads must not both pass a check only one of them fits.
        with self._publish_lock:
            self._check_capacity(incoming)
            staging_dir = self.staging / secrets.token_hex(8)
            staging_dir.mkdir(parents=True)
            try:
                variants = ["orig"]
                self._write(staging_dir / VARIANT_FILES["orig"], original)
                for variant in normalised.variants:
                    self._write(staging_dir / VARIANT_FILES[variant.kind],
                                variant.data)
                    variants.append(variant.kind)
                self._fsync_dir(staging_dir)
                os.rename(staging_dir, self.blobs / drop_id)
                self._fsync_dir(self.blobs)
            except Exception:
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise

            try:
                return self._insert(
                    drop_id=drop_id, batch=batch, filename=filename,
                    media_type=normalised.media_type, size=len(original),
                    width=normalised.width, height=normalised.height,
                    kind=normalised.kind, variants=variants,
                )
            except Exception:
                # C1 exemption: this id was generated in this call and the
                # directory it names has no row, so removing it is the
                # rollback rather than a request-driven deletion.
                self._remove_blob_dir(drop_id)
                raise

    def _insert(self, **row) -> Drop:
        created_at = self._now()
        conn = self._db()
        with conn:
            cursor = conn.execute(
                "INSERT INTO drops (id, batch, created_at, filename, media_type,"
                " size, width, height, kind, variants) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (row["drop_id"], row["batch"], created_at.isoformat(),
                 row["filename"], row["media_type"], row["size"], row["width"],
                 row["height"], row["kind"], ",".join(row["variants"])),
            )
        return Drop(id=row["drop_id"], seq=cursor.lastrowid, batch=row["batch"],
                    created_at=created_at, filename=row["filename"],
                    media_type=row["media_type"], size=row["size"],
                    width=row["width"], height=row["height"], kind=row["kind"],
                    variants=row["variants"])

    def delete(self, drop_id: str) -> bool:
        """Remove a drop. Returns False — touching nothing — for an id that is
        malformed or absent from the index."""
        try:
            validate_id(drop_id)
        except ValueError:
            return False
        conn = self._db()
        with conn:
            removed = conn.execute("DELETE FROM drops WHERE id = ?",
                                   (drop_id,)).rowcount
        if removed == 0:
            return False
        return self._remove_blob_dir(drop_id)

    def _remove_blob_dir(self, drop_id: str) -> bool:
        """Remove blobs/<id>, reporting failure rather than swallowing it.

        A sweep that reclaims nothing must not be able to report success
        (C9), so removal errors are counted and logged.
        """
        validate_id(drop_id)
        target = self.blobs / drop_id
        if target.resolve().parent != self.blobs.resolve():
            log.error("refusing to remove %s: outside the blob directory", target)
            return False
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            pass
        except OSError as error:
            log.error("could not remove %s: %s", target, error)
            return False
        return True

    # ----------------------------------------------------------------- reads

    def get(self, drop_id: str) -> Drop | None:
        try:
            validate_id(drop_id)
        except ValueError:
            return None
        row = self._db().execute(
            "SELECT * FROM drops WHERE id = ? AND created_at >= ?",
            (drop_id, self._cutoff().isoformat()),
        ).fetchone()
        return self._row(row) if row else None

    def blob_path(self, drop_id: str, variant: str) -> Path:
        validate_id(drop_id)
        if variant not in VARIANT_FILES:
            raise ValueError(f"not a variant: {variant!r}")
        return self.blobs / drop_id / VARIANT_FILES[variant]

    def list(self, *, after_seq: int | None = None, max_seq: int | None = None,
             since: datetime | None = None, until: datetime | None = None,
             batch: str | None = None, limit: int = 100,
             order: str = "asc") -> Page:
        """Page over drops within one read transaction.

        A paging client takes snapshot_max_seq from the first page and passes
        it back as max_seq, so it consumes a fixed boundary instead of
        chasing a frontier that moves with every new upload.
        """
        if order not in ("asc", "desc"):
            raise ValueError(f"not an order: {order!r}")

        where = ["created_at >= ?"]
        params: list[object] = [self._cutoff().isoformat()]
        if after_seq is not None:
            where.append("seq > ?")
            params.append(after_seq)
        if since is not None:
            where.append("created_at >= ?")
            params.append(since.astimezone(timezone.utc).isoformat())
        if until is not None:
            where.append("created_at <= ?")
            params.append(until.astimezone(timezone.utc).isoformat())
        if batch is not None:
            where.append("batch = ?")
            params.append(batch)
        clause = " AND ".join(where)

        conn = self._db()
        conn.execute("BEGIN")
        try:
            if max_seq is None:
                boundary = conn.execute(
                    f"SELECT COALESCE(MAX(seq), 0) AS s FROM drops WHERE {clause}",
                    params,
                ).fetchone()["s"]
            else:
                boundary = max_seq
            rows = conn.execute(
                f"SELECT * FROM drops WHERE {clause} AND seq <= ?"
                f" ORDER BY seq {'ASC' if order == 'asc' else 'DESC'} LIMIT ?",
                (*params, boundary, limit),
            ).fetchall()
        finally:
            conn.commit()
        return Page([self._row(r) for r in rows], boundary)

    def count(self) -> int:
        return self._db().execute(
            "SELECT COUNT(*) AS n FROM drops WHERE created_at >= ?",
            (self._cutoff().isoformat(),),
        ).fetchone()["n"]

    # ------------------------------------------------------------ operations

    def usage_bytes(self) -> int:
        total = 0
        for file in self.blobs.rglob("*"):
            try:
                if file.is_file():
                    total += file.stat().st_size
            except OSError:
                continue        # a concurrent sweep removed it; not our problem
        return total

    def sweep_expired(self) -> int:
        """Delete by age, then by count, oldest first.

        Physical deletion is separate from read visibility on purpose: the
        read filter makes 30 days true immediately, and this reclaims the
        bytes. An earlier design ran cleanup inline on upload, which
        implements 'clean up on the next upload' rather than expiry.
        """
        conn = self._db()
        cutoff = self._cutoff().isoformat()
        doomed = [r["id"] for r in conn.execute(
            "SELECT id FROM drops WHERE created_at < ?", (cutoff,))]

        excess = self.count() - self.settings.retention_max_drops
        if excess > 0:
            doomed += [r["id"] for r in conn.execute(
                "SELECT id FROM drops WHERE created_at >= ? ORDER BY seq ASC LIMIT ?",
                (cutoff, excess))]

        failures = 0
        for drop_id in doomed:
            if not self.delete(drop_id):
                failures += 1
        self._last_sweep = self._now()
        self._last_sweep_failures = failures
        return len(doomed) - failures

    def recover(self) -> tuple[int, int]:
        """Clear both kinds of debris a crash leaves.

        Staging directories are an interrupted publish. Blob directories with
        no row are a process killed between rename and insert — Python's
        exception cleanup never ran, so nothing else will ever reclaim them.
        """
        staging_removed = 0
        for entry in self.staging.iterdir():
            shutil.rmtree(entry, ignore_errors=True)
            staging_removed += 1

        known = {r["id"] for r in self._db().execute("SELECT id FROM drops")}
        blobs_removed = 0
        for entry in self.blobs.iterdir():
            if entry.is_dir() and entry.name not in known:
                # C1 exemption: the name was enumerated from blobs/ itself and
                # has no row, so this is recovery rather than a request-driven
                # deletion.
                shutil.rmtree(entry, ignore_errors=True)
                blobs_removed += 1
        return staging_removed, blobs_removed

    @property
    def last_sweep(self) -> datetime | None:
        return self._last_sweep

    @property
    def last_sweep_failures(self) -> int:
        return self._last_sweep_failures

    def writable(self) -> tuple[bool, str | None]:
        """Probe every location an upload needs, not just the root.

        Each probe carries a unique name: a shared '.writable' lets two
        concurrent health checks delete each other's file and report a
        failure that never happened.
        """
        token = secrets.token_hex(8)
        for label, directory in (("data root", self.settings.root),
                                 ("staging", self.staging),
                                 ("blobs", self.blobs)):
            probe = directory / f".writable-{token}"
            try:
                probe.write_bytes(b"ok")
                probe.unlink()
            except OSError as error:
                return False, f"{label} is not writable: {error}"
        try:
            self._db().execute("SELECT 1").fetchone()
        except sqlite3.Error as error:
            return False, f"index is not usable: {error}"
        return True, None

    # -------------------------------------------------------------- internal

    def _cutoff(self) -> datetime:
        return self._now() - timedelta(days=self.settings.retention_days)

    def _check_capacity(self, incoming: int) -> None:
        if self.usage_bytes() + incoming > self.settings.quota_bytes:
            raise QuotaExceeded("storage quota reached")
        free = shutil.disk_usage(self.settings.root).free
        if free - incoming < self.settings.free_reserve_bytes:
            raise QuotaExceeded("free space reserve would be breached")

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        with open(path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _row(row: sqlite3.Row) -> Drop:
        return Drop(id=row["id"], seq=row["seq"], batch=row["batch"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    filename=row["filename"], media_type=row["media_type"],
                    size=row["size"], width=row["width"], height=row["height"],
                    kind=row["kind"], variants=row["variants"].split(","))
