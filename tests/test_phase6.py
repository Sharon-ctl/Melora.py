"""Unit tests for Phase 6: Owner commands, privacy, backups, and JSON export."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from core.storage import Storage, StoredTrack
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
