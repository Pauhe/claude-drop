import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import images
from app.config import Settings
from app.store import Store


@pytest.fixture
def clock():
    return {"now": datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)}


def make(tmp_path, clock, **kw) -> Store:
    return Store(Settings(root=tmp_path, **kw), now=lambda: clock["now"])


def put(store, data, batch="b"):
    return store.publish(batch=batch, filename="s.png", original=data,
                         normalised=images.normalise(data))


def test_sweep_deletes_past_the_age_limit(tmp_path, clock, png_bytes):
    store = make(tmp_path, clock)
    old = put(store, png_bytes)
    clock["now"] += timedelta(days=31)
    fresh = put(store, png_bytes)

    assert store.sweep_expired() == 1
    assert not (store.blobs / old.id).exists()
    assert (store.blobs / fresh.id).exists()
    store.close()


def test_sweep_enforces_the_count_limit_oldest_first(tmp_path, clock, png_bytes):
    store = make(tmp_path, clock, retention_max_drops=3)
    published = [put(store, png_bytes) for _ in range(5)]
    assert store.sweep_expired() == 2
    assert [d.id for d in store.list().drops] == [d.id for d in published[2:]]
    store.close()


def test_sweep_at_exactly_the_limit_deletes_nothing(tmp_path, clock, png_bytes):
    store = make(tmp_path, clock, retention_max_drops=3)
    for _ in range(3):
        put(store, png_bytes)
    assert store.sweep_expired() == 0
    store.close()


def test_last_sweep_is_recorded(tmp_path, clock):
    store = make(tmp_path, clock)
    assert store.last_sweep is None
    store.sweep_expired()
    assert store.last_sweep == clock["now"]
    store.close()


def test_a_sweep_that_could_not_reclaim_reports_the_failure(tmp_path, clock,
                                                            png_bytes,
                                                            monkeypatch):
    """A sweep that deletes nothing must not refresh health as if it had."""
    store = make(tmp_path, clock, retention_max_drops=0)
    put(store, png_bytes)
    monkeypatch.setattr(store, "_remove_blob_dir", lambda drop_id: False)
    store.sweep_expired()
    assert store.last_sweep_failures == 1
    store.close()


def test_recover_removes_abandoned_staging(tmp_path, clock, png_bytes):
    store = make(tmp_path, clock)
    orphan = store.staging / "abandoned"
    orphan.mkdir(parents=True)
    (orphan / "orig").write_bytes(png_bytes)
    staging_removed, _ = store.recover()
    assert staging_removed == 1
    assert not orphan.exists()
    store.close()


def test_recover_removes_blobs_with_no_index_row(tmp_path, clock, png_bytes):
    store = make(tmp_path, clock)
    kept = put(store, png_bytes)
    orphan = store.blobs / ("a" * 32)
    orphan.mkdir()
    (orphan / "orig").write_bytes(png_bytes)

    _, blobs_removed = store.recover()
    assert blobs_removed == 1
    assert not orphan.exists()
    assert (store.blobs / kept.id).exists()
    store.close()


def test_a_hard_kill_between_rename_and_insert_is_recovered(tmp_path, png_bytes):
    """A killed process skips Python's exception cleanup entirely, leaving a
    blob directory nothing references. Only startup recovery reclaims it."""
    server_root = str(Path(__file__).resolve().parents[1])
    data_root = str(tmp_path / "data")
    sample = str(tmp_path / "sample.png")
    (tmp_path / "sample.png").write_bytes(png_bytes)

    script = f'''
import os, signal, sys
sys.path.insert(0, {server_root!r})
from pathlib import Path
from app import images
from app.config import Settings
from app.store import Store

store = Store(Settings(root=Path({data_root!r})))
def kill_instead(**row):
    os.kill(os.getpid(), signal.SIGKILL)
store._insert = kill_instead
data = open({sample!r}, "rb").read()
store.publish(batch="b", filename="s.png", original=data,
              normalised=images.normalise(data))
'''
    (tmp_path / "kill.py").write_text(script)
    result = subprocess.run([sys.executable, str(tmp_path / "kill.py")],
                            capture_output=True, timeout=120)
    assert result.returncode == -9, result.stderr.decode()

    store = Store(Settings(root=Path(data_root)))
    assert len(list(store.blobs.iterdir())) == 1        # the orphan is there
    _, blobs_removed = store.recover()
    assert blobs_removed == 1
    assert list(store.blobs.iterdir()) == []
    store.close()
