import threading

import pytest

from app import images
from app.config import Settings
from app.store import Drop, QuotaExceeded, Store


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(Settings(root=tmp_path))
    yield s
    s.close()


def put(store: Store, data: bytes, *, batch="b1", filename="shot.png") -> Drop:
    return store.publish(batch=batch, filename=filename, original=data,
                         normalised=images.normalise(data))


def test_ids_are_32_hex_characters(store, png_bytes):
    drop = put(store, png_bytes)
    assert len(drop.id) == 32
    assert all(c in "0123456789abcdef" for c in drop.id)


def test_sequence_increases(store, png_bytes):
    assert put(store, png_bytes).seq < put(store, png_bytes).seq


def test_all_variants_land_on_disk(store, png_bytes):
    drop = put(store, png_bytes)
    assert set(drop.variants) == {"orig", "view_png", "view_jpg"}
    for name in drop.variants:
        assert store.blob_path(drop.id, name).read_bytes()
    assert store.blob_path(drop.id, "orig").read_bytes() == png_bytes


def test_filename_is_metadata_only(store, png_bytes):
    drop = put(store, png_bytes, filename="../../etc/passwd")
    assert drop.filename == "../../etc/passwd"
    blobs = (store.settings.root / "blobs").resolve()
    assert store.blob_path(drop.id, "orig").resolve().is_relative_to(blobs)


@pytest.mark.parametrize("bad", ["..", ".", "", "../x", "a" * 31, "ZZZ" * 10,
                                 "%2e%2e", "../" * 5])
def test_a_malformed_id_is_rejected_before_any_path_is_built(store, bad):
    with pytest.raises(ValueError):
        store.blob_path(bad, "orig")


def test_delete_of_an_unknown_id_removes_nothing(store, png_bytes):
    kept = put(store, png_bytes)
    unknown = "f" * 32
    assert store.delete(unknown) is False
    # The decisive assertion: a bogus id must not take the data with it.
    assert store.blob_path(kept.id, "orig").exists()
    assert (store.settings.root / "blobs").exists()
    assert (store.settings.root / "db.sqlite3").exists()


def test_delete_of_a_traversal_id_removes_nothing(store, png_bytes):
    kept = put(store, png_bytes)
    assert store.delete("..") is False
    assert store.blob_path(kept.id, "orig").exists()
    assert (store.settings.root / "db.sqlite3").exists()


def test_a_trailing_newline_does_not_pass_id_validation(store, png_bytes):
    kept = put(store, png_bytes)
    assert store.get(kept.id + "\n") is None
    assert store.delete(kept.id + "\n") is False
    assert store.blob_path(kept.id, "orig").exists()


def test_delete_removes_row_and_blobs(store, png_bytes):
    drop = put(store, png_bytes)
    assert store.delete(drop.id) is True
    assert store.get(drop.id) is None
    assert not (store.settings.root / "blobs" / drop.id).exists()
    assert store.delete(drop.id) is False


def test_quota_rejects_the_upload_that_does_not_fit(tmp_path, png_bytes):
    probe = Store(Settings(root=tmp_path / "probe"))
    one = put(probe, png_bytes)
    stored = sum(probe.blob_path(one.id, v).stat().st_size for v in one.variants)
    probe.close()

    store = Store(Settings(root=tmp_path / "real", quota_bytes=stored + 10))
    kept = put(store, png_bytes)
    with pytest.raises(QuotaExceeded):
        put(store, png_bytes)
    assert store.get(kept.id) is not None      # never evicted to make room
    store.close()


def test_free_space_reserve_rejects(tmp_path, png_bytes):
    import shutil as sh
    store = Store(Settings(root=tmp_path,
                           free_reserve_bytes=sh.disk_usage(tmp_path).free))
    with pytest.raises(QuotaExceeded):
        put(store, png_bytes)
    store.close()


def test_a_failed_normalisation_still_stores_the_original(store, png_bytes):
    broken = png_bytes[:40]
    drop = put(store, broken, filename="broken.png")
    assert drop.kind == "failed"
    assert drop.variants == ["orig"]


def test_publish_failure_before_insert_leaves_nothing_behind(store, png_bytes,
                                                             monkeypatch):
    def explode(*a, **kw):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(store, "_insert", explode)
    with pytest.raises(RuntimeError):
        put(store, png_bytes)
    assert list((store.settings.root / "blobs").iterdir()) == []
    assert list((store.settings.root / "staging").iterdir()) == []


def test_concurrent_publishes_all_survive(store, png_bytes):
    results, errors = [], []
    lock = threading.Lock()

    def worker():
        try:
            drop = put(store, png_bytes)
            with lock:
                results.append(drop)
        except Exception as exc:       # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len({d.id for d in results}) == 8
    assert len({d.seq for d in results}) == 8


def test_batch_ids_are_unique(store):
    seen = {store.new_batch_id() for _ in range(50)}
    assert len(seen) == 50


def test_a_batch_collision_retries_instead_of_reusing(store, monkeypatch):
    """Forced collision, not luck: the generator must survive drawing an id
    that is already reserved."""
    taken = store.new_batch_id()
    draws = iter([taken, taken, "beefcafe"])
    monkeypatch.setattr("app.store.secrets.token_hex",
                        lambda n: next(draws) if n == 4 else "0" * (n * 2))
    assert store.new_batch_id() == "beefcafe"


def test_concurrent_batch_allocation_never_repeats(store):
    ids, lock = [], threading.Lock()

    def worker():
        value = store.new_batch_id()
        with lock:
            ids.append(value)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 16
