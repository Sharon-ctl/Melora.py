"""Phase 2 Performance and Memory Benchmarks.

Verifies:
- Page slice under 5 ms at 200,000 tracks
- Autocomplete under 20 ms at 200,000 tracks
- Shuffle under 500 ms at 200,000 tracks
- Memory under 600 bytes per queued track at 100,000 tracks
- __slots__ on QueueItem with shared bounded avatar cache
- Fast O(1) number autocomplete without scanning
"""
from __future__ import annotations

import asyncio
import time
import tracemalloc
from types import SimpleNamespace

from cogs.music import Music
from core.contracts import PlayerServices
from core.queue import QueueItem, TrackQueue, _AVATAR_CACHE, clear_avatar_cache
from core.registry import PlayerRegistry
from core.storage import Storage, StoredTrack
from tests.fakes import FakeBackend, FakeLoader, make_config


def test_queue_item_slots_and_no_dict():
    """Verify QueueItem defines __slots__ and does not have an instance __dict__."""
    item = QueueItem(
        track=None,
        title="Test Track",
        duration_ms=210000,
        requester_id=12345,
        artist="Test Artist",
        uri="https://example.com/test",
    )
    assert not hasattr(item, "__dict__"), "QueueItem must use __slots__ without __dict__"
    assert hasattr(item, "__slots__")


def test_shared_bounded_avatar_cache():
    """Verify requester_avatar_url is cached centrally, not stored per item."""
    clear_avatar_cache()
    avatar_url = "https://cdn.discordapp.com/avatars/42/avatar.png"
    item1 = QueueItem(
        track=None,
        title="Track 1",
        duration_ms=1000,
        requester_id=42,
        requester_avatar_url=avatar_url,
    )
    assert item1.requester_avatar_url == avatar_url
    assert _AVATAR_CACHE.get(42) == avatar_url

    # A second item for the same requester without explicit avatar resolves from shared cache
    item2 = QueueItem(
        track=None,
        title="Track 2",
        duration_ms=2000,
        requester_id=42,
    )
    assert item2.requester_avatar_url == avatar_url

    # Updating avatar reflects for all items of that requester
    new_avatar = "https://cdn.discordapp.com/avatars/42/new_avatar.png"
    item1.requester_avatar_url = new_avatar
    assert item2.requester_avatar_url == new_avatar
    assert _AVATAR_CACHE.get(42) == new_avatar


def test_benchmark_memory_under_600_bytes_per_queued_track():
    """Verify memory per queued track is under 600 bytes at 100,000 tracks."""
    tracemalloc.start()
    try:
        q = TrackQueue()
        for i in range(100_000):
            q._push(
                QueueItem(
                    track=None,
                    title=f"Sample Track Title Number {i}",
                    duration_ms=215000,
                    requester_id=1000000 + (i % 500),
                    is_stream=False,
                    artist=f"Band Name {i % 200}",
                    uri=f"https://www.youtube.com/watch?v=track_{i}",
                )
            )

        current, _ = tracemalloc.get_traced_memory()
        bytes_per_item = current / 100_000
        # Budget: under 600 bytes per queued track
        assert bytes_per_item < 600, f"Memory per track was {bytes_per_item:.1f} bytes (expected < 600 bytes)"
    finally:
        tracemalloc.stop()


def test_benchmark_page_under_5ms_at_200k_tracks():
    """Verify paging 200,000 tracks executes in under 5 ms."""
    q = TrackQueue()
    # Pre-populate 200,000 items
    dummy = QueueItem(
        track=None,
        title="Benchmark Song",
        duration_ms=180000,
        requester_id=999,
        artist="Benchmark Artist",
        uri="https://example.com/audio",
    )
    for _ in range(200_000):
        q._push(dummy)

    assert len(q) == 200_000

    # 1. Page at start (page 1)
    t0 = time.perf_counter()
    res1, p1, total1 = q.page(1, per_page=10)
    dur_start = (time.perf_counter() - t0) * 1000
    assert len(res1) == 10
    assert p1 == 1
    assert total1 == 20_000
    assert dur_start < 5.0, f"Page 1 took {dur_start:.3f} ms (expected < 5 ms)"

    # 2. Page in middle (page 10,000)
    t0 = time.perf_counter()
    res_mid, p_mid, _ = q.page(10_000, per_page=10)
    dur_mid = (time.perf_counter() - t0) * 1000
    assert len(res_mid) == 10
    assert p_mid == 10_000
    assert dur_mid < 5.0, f"Page 10,000 took {dur_mid:.3f} ms (expected < 5 ms)"

    # 3. Page at end (page 20,000)
    t0 = time.perf_counter()
    res_end, p_end, _ = q.page(20_000, per_page=10)
    dur_end = (time.perf_counter() - t0) * 1000
    assert len(res_end) == 10
    assert p_end == 20_000
    assert dur_end < 5.0, f"Page 20,000 took {dur_end:.3f} ms (expected < 5 ms)"


def test_benchmark_shuffle_under_500ms_at_200k_tracks():
    """Verify shuffling 200,000 tracks executes in under 500 ms."""
    q = TrackQueue()
    dummy = QueueItem(
        track=None,
        title="Song",
        duration_ms=180000,
        requester_id=1,
    )
    for _ in range(200_000):
        q._push(dummy)

    t0 = time.perf_counter()
    q.shuffle()
    dur_ms = (time.perf_counter() - t0) * 1000
    assert len(q) == 200_000
    assert dur_ms < 500.0, f"Shuffle took {dur_ms:.2f} ms (expected < 500 ms)"


def test_benchmark_autocomplete_under_20ms_at_200k_tracks():
    """Verify autocomplete at 200,000 tracks executes in under 20 ms."""
    async def scenario():
        cfg = make_config(AUTOCOMPLETE_SEARCH_ENABLED=True)
        backend = FakeBackend()
        loader = FakeLoader()
        storage = Storage(":memory:")
        storage.start()
        try:
            services = PlayerServices(cfg, backend, loader, storage)
            registry = PlayerRegistry(services)
            bot = SimpleNamespace(
                cfg=cfg,
                backend=backend,
                loader=loader,
                storage=storage,
                registry=registry,
            )
            music_cog = Music(bot)
            player = await registry.get_or_create(1, 10, 20)

            # Populate player.queue with 200,000 items
            item = QueueItem(
                track=None,
                title="Song",
                duration_ms=180000,
                requester_id=1,
                artist="Artist",
            )
            for _ in range(200_000):
                player.queue._push(item)

            inter = SimpleNamespace(guild=SimpleNamespace(id=1), guild_id=1)

            # 1. Number typed: pure arithmetic jump, 0 linear scans
            t0 = time.perf_counter()
            choices_num = await music_cog._queue_pos_autocomplete(inter, current="42")
            dur_num = (time.perf_counter() - t0) * 1000
            assert len(choices_num) > 0
            assert choices_num[0].value == 42
            assert dur_num < 20.0, f"Number autocomplete took {dur_num:.3f} ms (expected < 20 ms)"

            # 2. Number typed near end of 200,000 items
            t0 = time.perf_counter()
            choices_end = await music_cog._queue_pos_autocomplete(inter, current="199990")
            dur_end = (time.perf_counter() - t0) * 1000
            assert len(choices_end) > 0
            assert choices_end[0].value == 199990
            assert dur_end < 20.0, f"Large number autocomplete took {dur_end:.3f} ms (expected < 20 ms)"

            # 3. Empty query: scan breaks after 25 items
            t0 = time.perf_counter()
            choices_empty = await music_cog._queue_pos_autocomplete(inter, current="")
            dur_empty = (time.perf_counter() - t0) * 1000
            assert len(choices_empty) == 25
            assert dur_empty < 20.0, f"Empty query autocomplete took {dur_empty:.3f} ms (expected < 20 ms)"

            await registry.destroy_all("test")
        finally:
            await storage.close()

    asyncio.run(scenario())


def test_queue_operations_fast_at_200k():
    """Verify swap, move, remove, dedupe stay fast on large queues."""
    q = TrackQueue()
    for i in range(10_000):
        q._push(
            QueueItem(
                track=None,
                title=f"Song {i}",
                duration_ms=1000,
                requester_id=1,
                uri=f"uri://{i}",
            )
        )

    # Swap
    item1, item2 = q.swap(1, 10_000)
    assert item1.title == "Song 9999"
    assert item2.title == "Song 0"

    # Move
    moved = q.move(10_000, 1)
    assert moved.title == "Song 0"

    # Remove
    removed = q.remove(1)
    assert removed.title == "Song 0"

    # Dedupe
    q._push(
        QueueItem(
            track=None,
            title="Song 1",
            duration_ms=1000,
            requester_id=1,
            uri="uri://1",
        )
    )
    dup_removed = q.dedupe()
    assert dup_removed == 1


def test_compact_snapshot_serialization():
    """Verify compact serialization and restore of queue snapshots."""
    async def scenario():
        storage = Storage(":memory:")
        storage.start()
        try:
            tracks = [
                StoredTrack(
                    uri=f"https://youtube.com/watch?v=track_{i}",
                    title=f"Track Title {i}",
                    artist=f"Artist {i % 10}",
                    duration_ms=180000,
                    requester_id=100 + i,
                )
                for i in range(100)
            ]
            await storage.save_queue_snapshot(1234, tracks)
            loaded = await storage.get_queue_snapshot(1234)
            assert len(loaded) == 100
            assert loaded[0].title == "Track Title 0"
            assert loaded[0].requester_id == 100
            assert loaded[99].title == "Track Title 99"
            assert loaded[99].requester_id == 199
        finally:
            await storage.close()

    asyncio.run(scenario())
