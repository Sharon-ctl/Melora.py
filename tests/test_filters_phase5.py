"""Unit tests for Phase 5: Filters, EQ presets, vote skip, sleep timer, and autoplay."""
from __future__ import annotations

import pytest

from core.contracts import PlayerServices
from core.data_loader import load_eq_presets, load_filter_presets
from core.filters import (
    FilterValidationError,
    get_filter_types_for_preset,
    validate_and_build_eq,
    validate_and_build_filters,
)
from core.guild_player import GuildPlayer
from core.queue import QueueItem
from tests.fakes import FakeBackend, FakeLoader, make_config, make_track


@pytest.fixture
def services() -> PlayerServices:
    cfg = make_config()
    backend = FakeBackend()
    loader = FakeLoader()
    return PlayerServices(backend=backend, loader=loader, cfg=cfg)


def test_eq_presets_validation():
    eq_presets = load_eq_presets()
    assert "flat" in eq_presets
    assert "bassboost" in eq_presets
    assert "rock" in eq_presets

    for name, bands in eq_presets.items():
        eq = validate_and_build_eq(bands)
        assert eq is not None

    # Invalid band out of range
    with pytest.raises(FilterValidationError, match="out of range"):
        validate_and_build_eq([{"band": 15, "gain": 0.2}])

    # Invalid gain out of range
    with pytest.raises(FilterValidationError, match="out of range"):
        validate_and_build_eq([{"band": 0, "gain": 1.5}])

    with pytest.raises(FilterValidationError, match="out of range"):
        validate_and_build_eq([{"band": 0, "gain": -0.3}])


def test_filter_presets_validation():
    filter_presets = load_filter_presets()
    expected = ["bassboost", "nightcore", "vaporwave", "rotation", "karaoke", "tremolo", "vibrato"]
    for exp in expected:
        assert exp in filter_presets
        filters = validate_and_build_filters(filter_presets[exp])
        assert len(filters) >= 1
        types = get_filter_types_for_preset(filter_presets[exp])
        assert len(types) >= 1

    # Invalid timescale speed
    with pytest.raises(FilterValidationError, match="(?i)speed"):
        validate_and_build_filters({"timescale": {"speed": 0.05, "pitch": 1.0, "rate": 1.0}})

    # Invalid rotation hz
    with pytest.raises(FilterValidationError, match="(?i)rotation"):
        validate_and_build_filters({"rotation": {"rotation_hz": -1.0}})

    # Invalid tremolo depth
    with pytest.raises(FilterValidationError, match="(?i)depth"):
        validate_and_build_filters({"tremolo": {"frequency": 2.0, "depth": 1.5}})

    # Invalid vibrato freq
    with pytest.raises(FilterValidationError, match="(?i)frequency"):
        validate_and_build_filters({"vibrato": {"frequency": 20.0, "depth": 0.5}})


@pytest.mark.anyio
async def test_guild_player_eq_and_filters(services: PlayerServices):
    guild_id = 1001
    await services.backend.connect(guild_id, 100)
    player = GuildPlayer(guild_id, 100, 200, services)

    # Set EQ
    eq_presets = load_eq_presets()
    await player.set_eq("bassboost", eq_presets["bassboost"])
    assert player.current_eq == "bassboost"

    # Reset EQ
    await player.reset_eq()
    assert player.current_eq is None

    # Apply filter
    filter_presets = load_filter_presets()
    await player.apply_filter("nightcore", filter_presets["nightcore"])
    assert "nightcore" in player.applied_filters

    await player.apply_filter("vibrato", filter_presets["vibrato"])
    assert "vibrato" in player.applied_filters
    assert len(player.applied_filters) == 2

    # Remove one filter
    await player.remove_filter("vibrato", filter_presets["vibrato"])
    assert "vibrato" not in player.applied_filters
    assert "nightcore" in player.applied_filters

    # Reset all filters
    await player.reset_filters()
    assert len(player.applied_filters) == 0
    assert player.current_eq is None

    # Shutdown resets state
    player.applied_filters.add("nightcore")
    player.current_eq = "rock"
    player.shutdown()
    assert len(player.applied_filters) == 0
    assert player.current_eq is None


@pytest.mark.anyio
async def test_vote_skip(services: PlayerServices):
    guild_id = 1002
    await services.backend.connect(guild_id, 100)
    player = GuildPlayer(guild_id, 100, 200, services)

    t1 = make_track(1)
    t2 = make_track(2)
    item1 = QueueItem.from_track(t1, 10)
    item2 = QueueItem.from_track(t2, 20)
    await player.enqueue([item1, item2])
    assert player.current is not None
    assert player.current.title == "Track 1"

    # 4 humans listening -> needed = (4 // 2) + 1 = 3 votes
    skipped, votes, needed = await player.vote_skip(user_id=1, humans_count=4)
    assert not skipped
    assert votes == 1
    assert needed == 3

    # Duplicate vote from same user doesn't increase vote count
    skipped, votes, needed = await player.vote_skip(user_id=1, humans_count=4)
    assert not skipped
    assert votes == 1

    # Second user votes
    skipped, votes, needed = await player.vote_skip(user_id=2, humans_count=4)
    assert not skipped
    assert votes == 2

    # Third user reaches majority
    skipped, votes, needed = await player.vote_skip(user_id=3, humans_count=4)
    assert skipped
    # After skip, votes are cleared and next track plays
    assert len(player.votes) == 0
    assert player.current is not None
    assert player.current.title == "Track 2"

    player.shutdown()


@pytest.mark.anyio
async def test_sleep_timer(services: PlayerServices):
    player = GuildPlayer(1003, 100, 200, services)

    # Set sleep timer for 15 minutes
    player.set_sleep(15)
    assert player.has_timer("sleep")

    # Cancel sleep timer by passing 0
    player.set_sleep(0)
    assert not player.has_timer("sleep")

    # Set sleep and shutdown
    player.set_sleep(10)
    assert player.has_timer("sleep")
    player.shutdown()
    assert not player.has_timer("sleep")


@pytest.mark.anyio
async def test_autoplay_failure_limit(services: PlayerServices):
    guild_id = 1004
    await services.backend.connect(guild_id, 100)
    player = GuildPlayer(guild_id, 100, 200, services)
    player.set_autoplay(True)
    assert player.autoplay is True
    assert player.autoplay_failures == 0

    # Simulate 3 failures
    player.autoplay_failures = 3
    result = await player._try_autoplay_locked(None)
    assert result is False

    player.shutdown()
    assert player.autoplay is False
    assert player.autoplay_failures == 0


@pytest.mark.anyio
async def test_autoplay_disabled_by_default_stops_after_single_track(services: PlayerServices):
    guild_id = 1005
    await services.backend.connect(guild_id, 100)
    player = GuildPlayer(guild_id, 100, 200, services)
    assert player.autoplay is False

    track = make_track(1, title="Single Song")
    item = QueueItem.from_track(track, 42)
    res = await player.enqueue([item])
    assert res.started
    assert player.current is not None
    assert len(player.queue) == 0

    # Advancing when autoplay is False should stop audio and not queue anything
    started = await player._advance_locked()
    assert started is False
    assert player.current is None
    assert len(player.queue) == 0
    assert player.autoplay is False
    player.shutdown()


@pytest.mark.anyio
async def test_autoplay_populates_batch_into_queue(services: PlayerServices):
    guild_id = 1006
    await services.backend.connect(guild_id, 100)

    # Loader that returns 5 distinct search candidates
    class MultiSearchLoader(FakeLoader):
        async def load(self, gid: int, query: str):
            tracks = [make_track(100 + i, title=f"Autoplay Track {i}") for i in range(1, 6)]
            from types import SimpleNamespace
            return SimpleNamespace(tracks=tracks, kind="search")

    custom_services = PlayerServices(
        backend=services.backend,
        loader=MultiSearchLoader(),
        cfg=services.cfg,
    )
    player = GuildPlayer(guild_id, 100, 200, custom_services)
    player.set_autoplay(True)

    last_track = QueueItem.from_track(make_track(99, title="Initial Song"), 42)
    result = await player._try_autoplay_locked(last_track)
    assert result is True
    # 1 track started playing, 4 tracks remain in queue (batch of 5)
    assert player.current is not None
    assert player.current.title == "Autoplay Track 1"
    assert len(player.queue) == 4
    assert [item.title for item in player.queue] == [f"Autoplay Track {i}" for i in range(2, 6)]
    player.shutdown()


def test_lavalink_service_to_outcome_search_preserves_tracks():
    from unittest.mock import MagicMock
    from core.lavalink_service import LavalinkService
    from lavalink.server import LoadResult, LoadType

    svc = LavalinkService(MagicMock(), make_config())
    t1 = make_track(1, title="T1")
    t2 = make_track(2, title="T2")
    t3 = make_track(3, title="T3")

    # Search returns all tracks
    search_res = LoadResult(LoadType.SEARCH, [t1, t2, t3], None)
    search_outcome = svc._to_outcome(search_res, "ytsearch", False, "query")
    assert search_outcome.kind == "search"
    assert len(search_outcome.tracks) == 3

    # Direct track returns only 1 track
    track_res = LoadResult(LoadType.TRACK, [t1, t2], None)
    track_outcome = svc._to_outcome(track_res, "direct", False, None)
    assert track_outcome.kind == "track"
    assert len(track_outcome.tracks) == 1

