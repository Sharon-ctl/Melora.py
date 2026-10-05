"""Tests for Phase 1: 0 = unlimited semantics, rate limiter disable/re-enable, and button guards."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.queue import QueueItem, TrackQueue, validate_track
from core.storage import Storage, StoredTrack
from utils import messages
from utils.components_v2 import BaseCardView, _IN_FLIGHT_MESSAGES
from utils.errors import TrackTooLong
from utils.ratelimit import RateLimiter


def test_track_queue_unlimited():
    # max_size=0 and max_per_user=0 mean unlimited
    q = TrackQueue(max_size=0, max_per_user=0)
    for i in range(1000):
        item = QueueItem(track=None, title=f"Track {i}", duration_ms=1000, requester_id=1)
        assert q.can_add(1) is None
        q.add(item)
    assert len(q) == 1000
    assert q.count_for(1) == 1000


def test_validate_track_unlimited():
    track = SimpleNamespace(duration=10 * 3600 * 1000, is_stream=False)  # 10 hours
    # max_seconds=0 means unlimited duration
    validate_track(track, 0)

    # But with a limit, it raises TrackTooLong
    with pytest.raises(TrackTooLong):
        validate_track(track, 3600)


@pytest.mark.anyio
async def test_storage_limits_unlimited(tmp_path):
    db_file = tmp_path / "test.db"
    storage = Storage(db_file)
    storage.start()
    try:
        user_id = 12345
        # Add 100 favorites with limit=0 (unlimited)
        for i in range(100):
            st = StoredTrack(uri=f"uri:{i}", title=f"Title {i}", artist="Artist", duration_ms=1000)
            await storage.add_favorite(user_id, st, limit=0)
        favs = await storage.get_favorites(user_id)
        assert len(favs) == 100

        # Create 50 playlists with limit=0
        for i in range(50):
            await storage.create_playlist(user_id, f"pl_{i}", limit=0)
        pls = await storage.list_playlists(user_id)
        assert len(pls) == 50

        # Add 150 tracks to a playlist with limit=0
        for i in range(150):
            st = StoredTrack(uri=f"uri:{i}", title=f"Track {i}", artist="Artist", duration_ms=1000)
            await storage.add_playlist_track(user_id, "pl_0", st, limit=0)
        tracks = await storage.get_playlist_tracks(user_id, "pl_0")
        assert len(tracks) == 150
    finally:
        await storage.close()


def test_rate_limiter_disabled_fast_path():
    # Load with disabled configuration (as shipped in data/rate_limits.json)
    limiter = RateLimiter(config={"enabled": False, "buckets": {"commands": {"rate": 2, "per": 10.0}}})
    assert limiter.is_bucket_enabled("commands") is False

    for _ in range(50):
        allowed, retry = limiter.acquire_command(user_id=1, guild_id=1, command_name="play")
        assert allowed is True
        assert retry == 0.0

    # Ensure zero keys stored in internal tracking
    assert len(limiter._entries) == 0
    assert len(limiter._last_seen) == 0


def test_rate_limiter_re_enable():
    limiter = RateLimiter(config={"enabled": False, "buckets": {"commands": {"rate": 2, "per": 10.0, "enabled": False}}})

    # While disabled, unlimited calls allowed
    for _ in range(10):
        allowed, _ = limiter.acquire_command(user_id=1, guild_id=1, command_name="ping")
        assert allowed is True
    assert len(limiter._entries) == 0

    # Re-enable globally and bucket
    limiter.set_enabled(True)
    limiter.set_bucket_enabled("commands", True)
    assert limiter.is_bucket_enabled("commands") is True

    # Now enforce limit (2 per 10s)
    allowed1, _ = limiter.acquire_command(user_id=1, guild_id=1, command_name="ping")
    assert allowed1 is True
    allowed2, _ = limiter.acquire_command(user_id=1, guild_id=1, command_name="ping")
    assert allowed2 is True
    allowed3, retry = limiter.acquire_command(user_id=1, guild_id=1, command_name="ping")
    assert allowed3 is False
    assert retry > 0.0


@pytest.mark.anyio
async def test_button_silent_double_click_guard():
    view = BaseCardView()
    msg_id = 998877

    # Mock interactions on the same message
    interaction1 = SimpleNamespace(
        message=SimpleNamespace(id=msg_id),
        response=AsyncMock(),
        client=None,
        data={},
        command=None,
    )
    interaction1.response.is_done.return_value = False

    interaction2 = SimpleNamespace(
        message=SimpleNamespace(id=msg_id),
        response=AsyncMock(),
        client=None,
        data={},
        command=None,
    )
    interaction2.response.is_done.return_value = False

    mock_item = SimpleNamespace(
        _refresh_state=lambda i, d: None,
        _run_checks=AsyncMock(return_value=True),
        callback=AsyncMock(),
    )

    async def slow_callback(inter):
        # While first button is executing, verify second press gets quiet defer
        assert msg_id in _IN_FLIGHT_MESSAGES
        await view._scheduled_task(mock_item, interaction2)
        # interaction2 should have had response.defer() called
        interaction2.response.defer.assert_awaited_once()

    mock_item.callback.side_effect = slow_callback

    await view._scheduled_task(mock_item, interaction1)
    # After completion, message ID should be cleaned up
    assert msg_id not in _IN_FLIGHT_MESSAGES


def test_messages_unlimited_display():
    assert messages.max_queue_set(0) == "Set max queue to **Unlimited**"
    assert messages.max_queue_set(-1) == "Set max queue to **Unlimited**"
    assert messages.max_queue_set(50) == "Set max queue to **50 tracks**"

    assert messages.max_duration_set(0) == "Set max duration to **Unlimited**"
    assert messages.max_duration_set(60) == "Set max duration to **60 min**"
