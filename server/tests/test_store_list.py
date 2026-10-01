from datetime import datetime, timedelta, timezone

import pytest

from app import images
from app.config import Settings
from app.store import Store


@pytest.fixture
def clock():
    return {"now": datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)}


@pytest.fixture
def store(tmp_path, clock):
    s = Store(Settings(root=tmp_path), now=lambda: clock["now"])
    yield s
    s.close()


def put(store, data, *, batch="b1"):
    return store.publish(batch=batch, filename="s.png", original=data,
                         normalised=images.normalise(data))


def test_listing_is_ascending_by_sequence(store, png_bytes):
    a, b = put(store, png_bytes), put(store, png_bytes)
    assert [d.seq for d in store.list().drops] == [a.seq, b.seq]


def test_order_desc_returns_the_newest_first(store, png_bytes):
    a, b = put(store, png_bytes), put(store, png_bytes)
    assert [d.seq for d in store.list(order="desc").drops] == [b.seq, a.seq]


def test_desc_with_a_limit_returns_the_newest_not_the_oldest(store, png_bytes):
    published = [put(store, png_bytes) for _ in range(5)]
    page = store.list(order="desc", limit=2)
    assert [d.seq for d in page.drops] == [published[4].seq, published[3].seq]


def test_after_seq_excludes_what_was_seen(store, png_bytes):
    first, second = put(store, png_bytes), put(store, png_bytes)
    page = store.list(after_seq=first.seq)
    assert [d.id for d in page.drops] == [second.id]


def test_snapshot_is_returned_when_not_supplied(store, png_bytes):
    put(store, png_bytes)
    last = put(store, png_bytes)
    assert store.list(limit=1).snapshot_max_seq == last.seq


def test_a_supplied_snapshot_bounds_the_rows(store, png_bytes):
    first = put(store, png_bytes)
    put(store, png_bytes)
    page = store.list(max_seq=first.seq)
    assert [d.id for d in page.drops] == [first.id]
    assert page.snapshot_max_seq == first.seq


def test_a_drop_published_between_pages_is_not_in_the_bounded_page(store, png_bytes):
    published = [put(store, png_bytes) for _ in range(3)]
    first_page = store.list(limit=2)
    boundary = first_page.snapshot_max_seq

    intruder = put(store, png_bytes)          # arrives mid-pagination

    second_page = store.list(after_seq=first_page.drops[-1].seq, max_seq=boundary)
    ids = [d.id for d in second_page.drops]
    assert ids == [published[2].id]
    assert intruder.id not in ids


def test_the_intruder_is_still_available_to_the_next_pull(store, png_bytes):
    put(store, png_bytes)
    boundary = store.list().snapshot_max_seq
    intruder = put(store, png_bytes)
    later = store.list(after_seq=boundary)
    assert [d.id for d in later.drops] == [intruder.id]


def test_batch_filter(store, png_bytes):
    put(store, png_bytes, batch="alpha")
    wanted = put(store, png_bytes, batch="beta")
    assert [d.id for d in store.list(batch="beta").drops] == [wanted.id]


def test_since_and_until_bound_the_window(store, png_bytes, clock):
    early = put(store, png_bytes)
    clock["now"] += timedelta(hours=2)
    late = put(store, png_bytes)
    window = store.list(since=clock["now"] - timedelta(hours=1))
    assert [d.id for d in window.drops] == [late.id]
    earlier = store.list(until=clock["now"] - timedelta(hours=1))
    assert [d.id for d in earlier.drops] == [early.id]


def test_a_since_in_another_offset_is_normalised_before_comparison(store,
                                                                   png_bytes,
                                                                   clock):
    """String comparison of ISO timestamps only orders correctly once every
    value is in the same offset."""
    put(store, png_bytes)
    berlin = timezone(timedelta(hours=2))
    cutoff = (clock["now"] - timedelta(hours=1)).astimezone(berlin)
    assert len(store.list(since=cutoff).drops) == 1


def test_expired_drops_are_invisible_even_before_deletion(store, png_bytes, clock):
    drop = put(store, png_bytes)
    clock["now"] += timedelta(days=31)
    assert store.get(drop.id) is None
    assert store.list().drops == []
    assert store.blob_path(drop.id, "orig").exists()   # a read rule, not a delete


def test_count_ignores_expired(store, png_bytes, clock):
    put(store, png_bytes)
    clock["now"] += timedelta(days=31)
    put(store, png_bytes)
    assert store.count() == 1


def test_an_invalid_order_is_refused(store):
    with pytest.raises(ValueError):
        store.list(order="sideways")
