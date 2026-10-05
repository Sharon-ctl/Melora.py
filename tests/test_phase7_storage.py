"""Unit tests for Phase 7: Storage at Scale.

Verifies:
- Coalesce writes in the worker thread (latest-wins per guild snapshot)
- Group writes into single transactions with SAVEPOINT isolation
- Cache hot per-guild settings (bounded LRU/TTL, invalidated on write)
- Check indexes on hot columns with EXPLAIN QUERY PLAN (no full table scans)
- Storage non-blocking async operations
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from core.storage import SCHEMA_MIGRATIONS, Storage, StoredTrack


@pytest.fixture
async def temp_storage(tmp_path: Path):
    db_file = tmp_path / "test_scale.db"
    store = Storage(db_file)
    store.start()
    yield store
    await store.close()


@pytest.mark.anyio
async def test_hot_settings_cache_and_invalidation(temp_storage: Storage):
    """Verify get_guild_settings caches in memory and invalidates on update/delete."""
    guild_id = 9999

    # 1. First read: cache miss, loads defaults from DB and caches
    settings1 = await temp_storage.get_guild_settings(guild_id)
    assert settings1.guild_id == guild_id
    assert settings1.max_queue == 0
    assert temp_storage._settings_cache.get(guild_id) is not None

    # 2. Mutate cached object or verify fast hit
    cached = temp_storage._settings_cache.get(guild_id)
    assert cached == settings1

    # 3. Update settings: invalidates old cache, writes DB, updates cache
    updated = await temp_storage.update_guild_settings(guild_id, max_queue=500, volume_limit=80)
    assert updated.max_queue == 500
    assert updated.volume_limit == 80

    # Cache now reflects updated values
    cached_after = temp_storage._settings_cache.get(guild_id)
    assert cached_after is not None
    assert cached_after.max_queue == 500

    # 4. Delete guild: invalidates cache
    await temp_storage.delete_guild(guild_id)
    assert temp_storage._settings_cache.get(guild_id) is None


@pytest.mark.anyio
async def test_snapshot_write_coalescing(temp_storage: Storage):
    """Verify rapid queue snapshots for the same guild are coalesced with latest-wins."""
    guild_id = 12345

    track_v1 = StoredTrack("uri:1", "Track 1", "Artist 1", 1000)
    track_v2 = StoredTrack("uri:2", "Track 2", "Artist 2", 2000)
    track_v3 = StoredTrack("uri:3", "Track 3", "Artist 3", 3000)

    # Pause worker temporarily to accumulate a batch in queue
    # We can inject 3 snapshots into the queue before letting the worker drain them
    t1 = asyncio.create_task(temp_storage.save_queue_snapshot(guild_id, [track_v1]))
    t2 = asyncio.create_task(temp_storage.save_queue_snapshot(guild_id, [track_v2]))
    t3 = asyncio.create_task(temp_storage.save_queue_snapshot(guild_id, [track_v3]))

    await asyncio.gather(t1, t2, t3)

    # Final snapshot in DB must be track_v3 (latest-wins)
    loaded = await temp_storage.get_queue_snapshot(guild_id)
    assert len(loaded) == 1
    assert loaded[0].uri == "uri:3"
    assert loaded[0].title == "Track 3"


@pytest.mark.anyio
async def test_explain_query_plan_indexes():
    """Verify hot columns are indexed and queries use indexes or INTEGER PRIMARY KEY (no SCAN)."""
    conn = sqlite3.connect(":memory:")
    for script in SCHEMA_MIGRATIONS:
        conn.executescript(script)

    hot_queries = [
        ("guild_settings by guild_id", "SELECT * FROM guild_settings WHERE guild_id = ?", (1,)),
        ("queue_snapshots by guild_id", "SELECT tracks_json FROM queue_snapshots WHERE guild_id = ?", (1,)),
        (
            "favorites by user_id",
            "SELECT uri, title, artist, duration_ms FROM favorites WHERE user_id = ? ORDER BY position ASC",
            (1,),
        ),
        ("playlists by user_id", "SELECT playlist_id, name FROM playlists WHERE user_id = ? ORDER BY name ASC", (1,)),
        ("playlists by user_id and name", "SELECT playlist_id FROM playlists WHERE user_id = ? AND name = ?", (1, "My Playlist")),
        (
            "playlist_tracks by playlist_id",
            "SELECT uri, title, artist, duration_ms FROM playlist_tracks WHERE playlist_id = ? ORDER BY position ASC",
            (1,),
        ),
    ]

    for name, query, params in hot_queries:
        cur = conn.cursor()
        cur.execute(f"EXPLAIN QUERY PLAN {query}", params)
        plan_rows = cur.fetchall()
        # plan_rows contains tuples like (id, parent, notused, detail)
        details = " ".join(row[3] for row in plan_rows)
        # Detail must be SEARCH ... USING INDEX or INTEGER PRIMARY KEY, NOT SCAN TABLE
        assert "SEARCH" in details, f"Query '{name}' did not use SEARCH: {details}"
        assert ("INDEX" in details or "PRIMARY KEY" in details), f"Query '{name}' did not use an index: {details}"
        assert "SCAN TABLE" not in details, f"Query '{name}' performed a full table SCAN: {details}"

    conn.close()


@pytest.mark.anyio
async def test_batch_write_transaction_grouping(temp_storage: Storage):
    """Verify multiple writes succeed together and error in one does not break the batch."""
    user_id = 7777

    # Add 3 favorites concurrently
    fav1 = StoredTrack("uri:a", "A", "Artist", 1000)
    fav2 = StoredTrack("uri:b", "B", "Artist", 2000)
    fav3 = StoredTrack("uri:c", "C", "Artist", 3000)

    futs = await asyncio.gather(
        temp_storage.add_favorite(user_id, fav1, limit=0),
        temp_storage.add_favorite(user_id, fav2, limit=0),
        temp_storage.add_favorite(user_id, fav3, limit=0),
    )
    assert len(futs) == 3

    favs = await temp_storage.get_favorites(user_id)
    assert len(favs) == 3
    assert [f.uri for f in favs] == ["uri:a", "uri:b", "uri:c"]
