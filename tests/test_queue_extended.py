from types import SimpleNamespace

import pytest

from core.queue import QueueItem, TrackQueue
from utils.errors import QueueFull, UserLimitReached
from utils.text import format_history, parse_time_string


def make_item(user: int = 1, title: str = "t", duration: int = 1000) -> QueueItem:
    return QueueItem(track=SimpleNamespace(), title=title, duration_ms=duration, requester_id=user)


def test_insert_positions():
    q = TrackQueue(max_size=10, max_per_user=10)
    q.add(make_item(title="first"))
    q.add(make_item(title="second"))

    # Insert at position 1 (1-based)
    pos = q.insert(1, make_item(title="inserted_1"))
    assert pos == 1
    assert [i.title for i in q] == ["inserted_1", "first", "second"]

    # Insert in middle (position 2)
    pos = q.insert(2, make_item(title="inserted_mid"))
    assert pos == 2
    assert [i.title for i in q] == ["inserted_1", "inserted_mid", "first", "second"]

    # Insert beyond end clamps to end
    pos = q.insert(99, make_item(title="inserted_end"))
    assert pos == 5
    assert [i.title for i in q] == ["inserted_1", "inserted_mid", "first", "second", "inserted_end"]


def test_insert_limits():
    q = TrackQueue(max_size=2, max_per_user=2)
    q.add(make_item(user=1, title="1"))
    q.add(make_item(user=1, title="2"))

    with pytest.raises(QueueFull):
        q.insert(1, make_item(user=2, title="3"))

    q2 = TrackQueue(max_size=10, max_per_user=1)
    q2.add(make_item(user=1, title="1"))
    with pytest.raises(UserLimitReached):
        q2.insert(1, make_item(user=1, title="2"))


def test_push_front():
    q = TrackQueue(max_size=10, max_per_user=10)
    q.add(make_item(title="b"))
    q.push_front(make_item(title="a"))
    assert [i.title for i in q] == ["a", "b"]
    assert len(q) == 2


def test_remove_many():
    q = TrackQueue(max_size=10, max_per_user=10)
    for name in ["a", "b", "c", "d", "e"]:
        q.add(make_item(user=1, title=name))

    removed = q.remove_many(2, 2)
    assert [i.title for i in removed] == ["b", "c"]
    assert [i.title for i in q] == ["a", "d", "e"]
    assert q.count_for(1) == 3


def test_move_and_swap():
    q = TrackQueue(max_size=10, max_per_user=10)
    for name in ["a", "b", "c", "d"]:
        q.add(make_item(title=name))

    # Move from 1 ("a") to 3
    moved = q.move(1, 3)
    assert moved.title == "a"
    assert [i.title for i in q] == ["b", "c", "a", "d"]

    # Swap 1 and 4 ("b" and "d")
    item1, item2 = q.swap(1, 4)
    assert item1.title == "d"
    assert item2.title == "b"
    assert [i.title for i in q] == ["d", "c", "a", "b"]

    # Bounds check
    with pytest.raises(IndexError):
        q.move(0, 2)
    with pytest.raises(IndexError):
        q.move(1, 10)
    with pytest.raises(IndexError):
        q.swap(1, 10)


def test_dedupe():
    q = TrackQueue(max_size=10, max_per_user=10)
    q.add(make_item(user=1, title="Track A"))
    q.add(make_item(user=1, title="track a"))
    q.add(make_item(user=2, title="Track B"))
    q.add(make_item(user=1, title="TRACK A"))
    q.add(make_item(user=2, title="Track C"))
    q.add(make_item(user=2, title="track b"))

    removed_count = q.dedupe()
    assert removed_count == 3
    assert [i.title for i in q] == ["Track A", "Track B", "Track C"]
    assert q.count_for(1) == 1
    assert q.count_for(2) == 2


def test_skipto():
    q = TrackQueue(max_size=10, max_per_user=10)
    for name in ["a", "b", "c", "d"]:
        q.add(make_item(title=name))

    # Target is 'c' (1-based position 3), drops 1 and 2 ('a', 'b')
    skipped = q.skipto(3)
    assert [i.title for i in skipped] == ["a", "b"]
    assert [i.title for i in q] == ["c", "d"]


def test_history_bounded():
    q = TrackQueue(max_size=10, max_per_user=10, history_size=3)
    for name in ["h1", "h2", "h3", "h4"]:
        q.record_history(make_item(title=name))

    hist = q.get_history()
    assert len(hist) == 3
    # Most recent first
    assert [i.title for i in hist] == ["h4", "h3", "h2"]

    popped = q.pop_history()
    assert popped is not None
    assert popped.title == "h4"
    assert [i.title for i in q.get_history()] == ["h3", "h2"]


def test_parse_time_string():
    assert parse_time_string("45") == 45
    assert parse_time_string("1:30") == 90
    assert parse_time_string("01:30") == 90
    assert parse_time_string("1:02:03") == 3723
    assert parse_time_string("invalid") is None
    assert parse_time_string("-5") is None
    assert parse_time_string("1:2:3:4") is None


def test_format_history():
    entries = [(1, "Song 1", 60000), (2, "Song 2", 120000)]
    formatted = format_history(entries, total=2)
    assert "Song 1" in formatted
    assert "Song 2" in formatted
    assert "Playback history (last 2 tracks):" in formatted
