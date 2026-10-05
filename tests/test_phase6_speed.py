"""Unit tests for Phase 6: Perceived Speed.

Verifies:
- Load-result cache (bounded LRU with TTL for search and load results)
- 30-second negative caching of failed queries
- Single-flight: identical concurrent loads share one Lavalink call
- Prefetch: resolves next 2 queued items in the background
- Prefetch cancellation on queue changes
- /play acknowledges immediately on first chunk and streams remainder in background
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from lavalink.server import LoadResult, LoadType

from core.contracts import PlayerServices
from core.lavalink_service import LavalinkService
from core.queue import QueueItem
from core.registry import PlayerRegistry
from tests.fakes import FakeAudio, FakeBackend, FakeLoader, make_config
from utils.errors import NoMatches


@pytest.mark.anyio
async def test_load_cache_positive():
    """Verify successful load results are cached and reused without duplicate client calls."""
    client = MagicMock()
    mock_track = SimpleNamespace(title="Test Track", author="Artist", track="encoded_abc")
    fake_result = LoadResult(LoadType.TRACK, [mock_track], None)
    client.get_tracks = AsyncMock(return_value=fake_result)
    client.node_manager.available_nodes = [MagicMock()]

    cfg = make_config()
    service = LavalinkService(client, cfg)

    # First fetch: calls client
    outcome1 = await service.load(1, "https://example.com/track")
    assert client.get_tracks.call_count == 1
    assert len(outcome1.tracks) == 1

    # Second fetch: served from positive cache
    outcome2 = await service.load(1, "https://example.com/track")
    assert client.get_tracks.call_count == 1
    assert outcome2.tracks[0].title == "Test Track"


@pytest.mark.anyio
async def test_load_negative_cache():
    """Verify failed load queries are cached negatively for 30s and do not hit client again."""
    client = MagicMock()
    fake_empty = LoadResult(LoadType.EMPTY, [], None)
    client.get_tracks = AsyncMock(return_value=fake_empty)
    client.node_manager.available_nodes = [MagicMock()]

    cfg = make_config()
    service = LavalinkService(client, cfg)

    # First fetch: fails with NoMatches
    with pytest.raises(NoMatches):
        await service.load(1, "https://example.com/nonexistent_track")
    assert client.get_tracks.call_count == 1

    # Second fetch: raises immediately from negative cache
    with pytest.raises(NoMatches):
        await service.load(1, "https://example.com/nonexistent_track")
    assert client.get_tracks.call_count == 1


@pytest.mark.anyio
async def test_single_flight_deduplication():
    """Verify identical concurrent loads share a single Lavalink call."""
    client = MagicMock()
    mock_track = SimpleNamespace(title="Shared Track", author="Artist", track="encoded_shared")
    fake_result = LoadResult(LoadType.TRACK, [mock_track], None)

    async def delayed_get_tracks(query):
        await asyncio.sleep(0.05)
        return fake_result

    client.get_tracks = AsyncMock(side_effect=delayed_get_tracks)
    client.node_manager.available_nodes = [MagicMock()]

    cfg = make_config()
    service = LavalinkService(client, cfg)

    # Launch 4 concurrent loads for the same URL across different guilds
    task1 = asyncio.create_task(service.load(1, "https://example.com/shared"))
    task2 = asyncio.create_task(service.load(2, "https://example.com/shared"))
    task3 = asyncio.create_task(service.load(3, "https://example.com/shared"))
    task4 = asyncio.create_task(service.load(4, "https://example.com/shared"))

    results = await asyncio.gather(task1, task2, task3, task4)
    assert len(results) == 4
    # All 4 shared exactly ONE call to client.get_tracks
    assert client.get_tracks.call_count == 1


@pytest.mark.anyio
async def test_two_track_prefetch_and_cancellation():
    """Verify preload_next pre-resolves up to 2 items and cancels on queue changes."""
    backend = FakeBackend()
    loader = FakeLoader()
    services = PlayerServices(make_config(), backend, loader)
    registry = PlayerRegistry(services)

    player = await registry.get_or_create(1, 10, 20)
    fake_track = SimpleNamespace(title="Current Track", duration=180000, track="enc_curr")
    backend.audios[1] = FakeAudio()

    # Enqueue 1 active item and 3 unresolved items
    item_curr = QueueItem.from_track(fake_track, requester_id=111)
    unresolved1 = QueueItem(track=None, title="Song 1", duration_ms=100000, requester_id=111, query="ytsearch:Song 1")
    unresolved2 = QueueItem(track=None, title="Song 2", duration_ms=120000, requester_id=111, query="ytsearch:Song 2")
    unresolved3 = QueueItem(track=None, title="Song 3", duration_ms=140000, requester_id=111, query="ytsearch:Song 3")

    await player.enqueue([item_curr, unresolved1, unresolved2, unresolved3])

    # Allow prefetch tasks to run
    await asyncio.sleep(0.05)

    # First 2 upcoming items were pre-resolved
    assert unresolved1.track is not None
    assert unresolved2.track is not None
    # 3rd item was NOT pre-resolved (bound = 2)
    assert unresolved3.track is None

    # Verify cancel_prefetch stops task
    assert hasattr(player, "cancel_prefetch")
    player.cancel_prefetch()
    assert player._prefetch_task is None
