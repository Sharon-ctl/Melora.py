import random
from types import SimpleNamespace

import pytest

from core.queue import LoopMode, QueueItem, TrackQueue, validate_track
from utils.errors import QueueFull, TrackTooLong, UserLimitReached


def item(user: int = 1, title: str = "t", duration: int = 1000) -> QueueItem:
    return QueueItem(track=SimpleNamespace(), title=title, duration_ms=duration, requester_id=user)


def titles(queue: TrackQueue) -> list[str]:
    return [i.title for i in queue]


def test_fifo_order():
    q = TrackQueue(10, 10)
    for name in "abc":
        q.add(item(title=name))
    assert q.next_item(None).title == "a"
    assert q.next_item(None).title == "b"
    assert q.next_item(None).title == "c"
    assert q.next_item(None) is None


def test_max_size_rejects():
    q = TrackQueue(2, 5)
    q.add(item(1))
    q.add(item(2))
    with pytest.raises(QueueFull):
        q.add(item(3))


def test_per_user_cap_rejects_only_that_user():
    q = TrackQueue(10, 2)
    q.add(item(1))
    q.add(item(1))
    with pytest.raises(UserLimitReached):
        q.add(item(1))
    q.add(item(2))
    assert len(q) == 3


def test_per_user_count_released_when_played():
    q = TrackQueue(10, 1)
    q.add(item(1))
    q.next_item(None)
    q.add(item(1))
    assert q.count_for(1) == 1


def test_add_many_reports_skipped_and_reason():
    q = TrackQueue(3, 10)
    result = q.add_many([item(1, str(n)) for n in range(5)])
    assert (result.added, result.skipped, result.reason) == (3, 2, "full")
    q2 = TrackQueue(10, 2)
    result = q2.add_many([item(1) for _ in range(4)])
    assert (result.added, result.skipped, result.reason) == (2, 2, "user")


def test_loop_off_does_not_repeat():
    q = TrackQueue(10, 10)
    current = item(title="cur")
    q.add(item(title="next"))
    assert q.next_item(current).title == "next"
    assert q.next_item(None) is None


def test_loop_track_repeats_unless_skipped_or_failed():
    q = TrackQueue(10, 10)
    q.loop = LoopMode.TRACK
    current = item(title="cur")
    q.add(item(title="next"))
    assert q.next_item(current) is current
    assert q.next_item(current, skipped=True).title == "next"
    q.add(item(title="after"))
    assert q.next_item(current, failed=True).title == "after"


def test_loop_queue_cycles_and_drops_failed():
    q = TrackQueue(10, 10)
    q.loop = LoopMode.QUEUE
    a, b = item(title="a"), item(title="b")
    q.add(b)
    assert q.next_item(a) is b
    assert titles(q) == ["a"]
    assert q.next_item(b).title == "a"
    assert titles(q) == ["b"]
    failed = item(title="bad")
    assert q.next_item(failed, failed=True).title == "b"
    assert len(q) == 0


def test_loop_queue_single_track_replays_itself():
    q = TrackQueue(10, 10)
    q.loop = LoopMode.QUEUE
    only = item(title="only")
    assert q.next_item(only) is only


def test_remove_and_bounds():
    q = TrackQueue(10, 10)
    for name in "abc":
        q.add(item(title=name))
    assert q.remove(2).title == "b"
    assert titles(q) == ["a", "c"]
    with pytest.raises(IndexError):
        q.remove(0)
    with pytest.raises(IndexError):
        q.remove(3)


def test_clear_resets_counts():
    q = TrackQueue(10, 1)
    q.add(item(1))
    assert q.clear() == 1
    q.add(item(1))
    assert len(q) == 1


def test_shuffle_keeps_items_and_counts():
    q = TrackQueue(50, 50)
    for n in range(20):
        q.add(item(n % 3, str(n)))
    before = sorted(titles(q))
    q.shuffle(random.Random(7))
    assert sorted(titles(q)) == before
    assert q.count_for(0) == 7


def test_page_clamps_and_sizes():
    q = TrackQueue(100, 100)
    for n in range(25):
        q.add(item(title=str(n)))
    entries, page, pages = q.page(1, 10)
    assert (page, pages, len(entries)) == (1, 3, 10)
    assert entries[0][0] == 1
    entries, page, pages = q.page(99, 10)
    assert (page, len(entries), entries[0][0]) == (3, 5, 21)
    empty = TrackQueue(5, 5)
    assert empty.page(4, 10) == ([], 1, 1)


def test_validate_track_limits():
    ok = SimpleNamespace(duration=60_000, is_stream=False)
    validate_track(ok, 120)
    with pytest.raises(TrackTooLong):
        validate_track(SimpleNamespace(duration=200_000, is_stream=False), 120)
    with pytest.raises(TrackTooLong):
        validate_track(SimpleNamespace(duration=1000, is_stream=True), 120)


def test_from_track_cleans_and_bounds_title():
    track = SimpleNamespace(title="  a\nb\t" + "x" * 500, duration=5, is_stream=False)
    built = QueueItem.from_track(track, 9)
    assert "\n" not in built.title and len(built.title) <= 200
    assert built.requester_id == 9
