"""Unit tests for Phase 6: Owner commands, privacy, backups, and JSON export."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from core.storage import Storage, StoredTrack, StorageError
from scripts.export_json import export_database
from utils.errors import ErrorRingBuffer


def test_error_ring_buffer():
    buf = ErrorRingBuffer(maxlen=5)
    assert len(buf) == 0

    # Add 7 items to a buffer of maxlen 5
    for i in range(1, 8):
        buf.record(f"err-{i}", "play", f"Error summary {i}")

    assert len(buf) == 5
    recent = buf.get_recent(3)
    assert len(recent) == 3
    # Newest first
    assert recent[0].error_id == "err-7"
    assert recent[1].error_id == "err-6"
    assert recent[2].error_id == "err-5"

    all_recent = buf.get_recent(10)
    assert len(all_recent) == 5
    assert all_recent[-1].error_id == "err-3"

    buf.clear()
    assert len(buf) == 0


@pytest.mark.anyio
async def test_database_backup_and_rotation(tmp_path: Path):
    db_path = tmp_path / "bot.db"
    store = Storage(db_path)
    store.start()

    try:
        # Put some data
        await store.update_guild_settings(100, volume_limit=75)
        backup_dir = tmp_path / "backups"

        # Create 9 backups keeping only 7
        for i in range(9):
            dest = backup_dir / f"bot_2026010{i}_000000.db"
            await store.backup(dest)

        # Call perform_daily_backup which rotates to keep 7
        await store.perform_daily_backup(backup_dir, keep=7)

        remaining = list(backup_dir.glob("bot_*.db"))
        assert len(remaining) <= 7
    finally:
        await store.close()


@pytest.mark.anyio
async def test_delete_guild_data_and_user_reset(tmp_path: Path):
    db_path = tmp_path / "test_del.db"
    store = Storage(db_path)
    store.start()

    try:
        guild_id = 999
        user_id = 777

        # Add guild settings and snapshot
        await store.update_guild_settings(guild_id, volume_limit=60, voice_247_channel_id=123)
        await store.save_queue_snapshot(
            guild_id,
            [StoredTrack(uri="http://a", title="A", artist="B", duration_ms=100)],
        )

        # Add user favorite and playlist
        await store.add_favorite(
            user_id,
            StoredTrack(uri="http://fav", title="Fav", artist="Art", duration_ms=200),
            limit=10,
        )
        await store.create_playlist(user_id, "My List", limit=10)
        await store.add_playlist_track(
            user_id,
            "My List",
            StoredTrack(uri="http://pt", title="Pt", artist="Art", duration_ms=150),
            limit=10,
        )

        # Verify before deletion
        g_set = await store.get_guild_settings(guild_id)
        assert g_set.volume_limit == 60
        favs = await store.get_favorites(user_id)
        assert len(favs) == 1
        pls = await store.list_playlists(user_id)
        assert len(pls) == 1

        # Delete guild data
        await store.delete_guild_data(guild_id)
        g_after = await store.get_guild_settings(guild_id)
        assert g_after.volume_limit == 100  # Default restored
        snap_after = await store.get_queue_snapshot(guild_id)
        assert snap_after == []

        # Reset user data
        await store.reset_user_data(user_id)
        favs_after = await store.get_favorites(user_id)
        assert favs_after == []
        pls_after = await store.list_playlists(user_id)
        assert pls_after == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_shared_playlist_is_read_only_for_recipient(tmp_path: Path):
    store = Storage(tmp_path / "shared.db")
    store.start()
    owner_id, recipient_id = 101, 202
    original = StoredTrack(uri="https://example.test/one", title="One", artist="Artist", duration_ms=60_000)
    try:
        await store.create_playlist(owner_id, "Favorites", limit=10)
        await store.add_playlist_track(owner_id, "Favorites", original, limit=10)
        await store.share_playlist(owner_id, "Favorites", recipient_id)

        shared = await store.get_shared_playlist_tracks(recipient_id, owner_id, "Favorites")
        assert shared == [original]
        with pytest.raises(StorageError, match="Playlist not found"):
            await store.add_playlist_track(recipient_id, "Favorites", original, limit=10)
        with pytest.raises(StorageError, match="Playlist not found"):
            await store.remove_playlist_track(recipient_id, "Favorites", 1)
        assert await store.get_shared_playlist_tracks(recipient_id, owner_id, "Favorites") == [original]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_listening_stats_retains_most_recent_track_on_count_tie(tmp_path: Path):
    store = Storage(tmp_path / "stats.db")
    store.start()
    try:
        await store.record_user_listening_stats(303, "Zulu", "Artist", 30_000, max_tracks=1)
        await store.record_user_listening_stats(303, "Alpha", "Artist", 30_000, max_tracks=1)
        stats = await store.get_user_listening_stats(303)
        assert stats["tracks"] == [("Alpha", "Artist", 1)]

        await store.record_user_listening_stats(303, "Zulu", "Artist", 30_000, max_tracks=1)
        await store.record_user_listening_stats(303, "Zulu", "Artist", 30_000, max_tracks=1)
        stats = await store.get_user_listening_stats(303)
        assert stats["tracks"] == [("Zulu", "Artist", 2)]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_listening_stats_new_track_can_build_count_when_history_is_full(tmp_path: Path):
    store = Storage(tmp_path / "full_stats.db")
    store.start()
    try:
        for title in ("Alpha", "Beta"):
            await store.record_user_listening_stats(404, title, "Artist", 30_000, max_tracks=2)
            await store.record_user_listening_stats(404, title, "Artist", 30_000, max_tracks=2)

        await store.record_user_listening_stats(404, "Zulu", "Artist", 30_000, max_tracks=2)
        await store.record_user_listening_stats(404, "Zulu", "Artist", 30_000, max_tracks=2)
        stats = await store.get_user_listening_stats(404)
        assert ("Zulu", "Artist", 2) in stats["tracks"]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_json_export_script(tmp_path: Path):
    db_path = tmp_path / "export_test.db"
    store = Storage(db_path)
    store.start()

    try:
        await store.update_guild_settings(888, dj_only=True)
        await store.add_favorite(
            555,
            StoredTrack(uri="http://exp", title="Exp Song", artist="Exp Artist", duration_ms=300000),
            limit=5,
        )
    finally:
        await store.close()

    out_json = tmp_path / "out.json"
    exported = export_database(db_path, out_json)
    assert exported.exists()

    with open(exported, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert "guild_settings" in data
    assert "favorites" in data
    assert "playlists" in data
    assert "queue_snapshots" in data

    assert len(data["guild_settings"]) >= 1
    assert data["guild_settings"][0]["guild_id"] == 888
    assert len(data["favorites"]) >= 1
    assert data["favorites"][0]["title"] == "Exp Song"
