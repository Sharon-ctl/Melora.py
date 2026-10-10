import asyncio
from pathlib import Path

import pytest

from core.data_loader import (
    DataValidationError,
    load_eq_presets,
    load_filter_presets,
    load_matching_rules,
)
from core.storage import Storage, StorageError, StoredTrack


def test_data_loader_valid():
    eq = load_eq_presets()
    assert "flat" in eq and "bassboost" in eq
    assert len(eq["flat"]) == 15

    filters = load_filter_presets()
    assert "nightcore" in filters and "bassboost" in filters

    rules = load_matching_rules()
    assert "penalized_keywords" in rules
    assert "match_threshold" in rules


def test_data_loader_invalid_eq(tmp_path: Path):
    bad_eq_file = tmp_path / "bad_eq.json"
    bad_eq_file.write_text('{"bad": [{"band": 99, "gain": 0.0}]}', encoding="utf-8")
    with pytest.raises(DataValidationError):
        load_eq_presets(bad_eq_file)


def run_async(coro):
    return asyncio.run(coro)


def test_storage_lifecycle_and_crud(tmp_path: Path):
    async def scenario():
        db_path = tmp_path / "test.db"
        storage = Storage(db_path)
        storage.start()

        # Guild settings
        settings = await storage.get_guild_settings(123)
        assert settings.guild_id == 123
        assert settings.default_volume == 100

        updated = await storage.update_guild_settings(123, autoplay=True, volume_limit=80)
        assert updated.autoplay is True
        assert updated.volume_limit == 80

        # Favorites
        track1 = StoredTrack(uri="http://example.com/1", title="Song 1", artist="Artist 1", duration_ms=180000)
        track2 = StoredTrack(uri="http://example.com/2", title="Song 2", artist="Artist 2", duration_ms=200000)
        pos1 = await storage.add_favorite(42, track1, limit=5)
        pos2 = await storage.add_favorite(42, track2, limit=5)
        assert pos1 == 1 and pos2 == 2

        favs = await storage.get_favorites(42)
        assert len(favs) == 2
        assert favs[0].title == "Song 1"

        with pytest.raises(StorageError):
            await storage.add_favorite(42, track1, limit=2)

        removed = await storage.remove_favorite(42, 1)
        assert removed.title == "Song 1"
        favs_after = await storage.get_favorites(42)
        assert len(favs_after) == 1
        assert favs_after[0].title == "Song 2"

        cleared = await storage.clear_favorites(42)
        assert cleared == 1
        assert len(await storage.get_favorites(42)) == 0

        # Playlists
        pl_id = await storage.create_playlist(42, "My List", limit=3)
        assert pl_id > 0
        with pytest.raises(StorageError):
            await storage.create_playlist(42, "My List", limit=3)

        assert await storage.list_playlists(42) == ["My List"]

        # Playlist tracks
        t_pos = await storage.add_playlist_track(42, "My List", track1, limit=10)
        assert t_pos == 1
        tracks = await storage.get_playlist_tracks(42, "My List")
        assert len(tracks) == 1 and tracks[0].title == "Song 1"

        # Rename playlist
        renamed = await storage.rename_playlist(42, "My List", "New List")
        assert renamed is True
        assert await storage.list_playlists(42) == ["New List"]

        # Queue snapshot
        await storage.save_queue_snapshot(123, [track1, track2])
        snapshot = await storage.get_queue_snapshot(123)
        assert len(snapshot) == 2
        assert snapshot[0].title == "Song 1"
        await storage.delete_queue_snapshot(123)
        assert len(await storage.get_queue_snapshot(123)) == 0

        # Backup
        backup_path = tmp_path / "backup.db"
        await storage.backup(backup_path)
        assert backup_path.exists()

        # Export
        export_data = await storage.export_all_json()
        assert "guild_settings" in export_data
        assert "playlists" in export_data

        # User data reset
        await storage.reset_user_data(42)
        assert len(await storage.list_playlists(42)) == 0

        # Clean close
        await storage.close()

    run_async(scenario())


def test_user_play_history_storage(tmp_path: Path):
    async def scenario():
        db_path = tmp_path / "history_test.db"
        storage = Storage(db_path)
        storage.start()

        user_id = 12345
        other_user = 67890

        # Initially empty
        entries = await storage.get_user_play_history(user_id)
        assert entries == []

        # Record 3 entries
        await storage.record_user_play_history(
            user_id, title="Track 1", artist="Artist 1", uri="https://example.com/1", max_entries=3
        )
        await asyncio.sleep(0.01)
        await storage.record_user_play_history(
            user_id, title="Track 2", artist="Artist 2", uri="https://example.com/2", max_entries=3
        )
        await asyncio.sleep(0.01)
        await storage.record_user_play_history(
            user_id, title="Track 3", artist="Artist 3", uri="https://example.com/3", max_entries=3
        )

        entries = await storage.get_user_play_history(user_id)
        assert len(entries) == 3
        # Newest first
        assert entries[0].title == "Track 3"
        assert entries[1].title == "Track 2"
        assert entries[2].title == "Track 1"

        # Cached read
        cached_entries = await storage.get_user_play_history_cached(user_id)
        assert len(cached_entries) == 3
        assert cached_entries[0].title == "Track 3"

        # Replaying Track 1 moves it to top (de-duplication)
        await asyncio.sleep(0.01)
        await storage.record_user_play_history(
            user_id, title="Track 1", artist="Artist 1", uri="https://example.com/1", max_entries=3
        )
        entries_after_replay = await storage.get_user_play_history(user_id)
        assert len(entries_after_replay) == 3
        assert entries_after_replay[0].title == "Track 1"
        assert entries_after_replay[1].title == "Track 3"
        assert entries_after_replay[2].title == "Track 2"

        # Pruning: record a 4th unique track with max_entries=3 -> oldest (Track 2) is pruned
        await asyncio.sleep(0.01)
        await storage.record_user_play_history(
            user_id, title="Track 4", artist="Artist 4", uri="https://example.com/4", max_entries=3
        )
        entries_after_prune = await storage.get_user_play_history(user_id)
        assert len(entries_after_prune) == 3
        assert entries_after_prune[0].title == "Track 4"
        assert entries_after_prune[1].title == "Track 1"
        assert entries_after_prune[2].title == "Track 3"
        assert all(e.title != "Track 2" for e in entries_after_prune)

        # Other user has independent history
        await storage.record_user_play_history(
            other_user, title="Other Track", artist="Other Artist", uri="https://example.com/other", max_entries=3
        )
        other_entries = await storage.get_user_play_history(other_user)
        assert len(other_entries) == 1
        assert other_entries[0].title == "Other Track"

        # Guild removal / delete_guild_data does NOT delete user play history
        await storage.delete_guild_data(999)
        assert len(await storage.get_user_play_history(user_id)) == 3

        # User reset: deletes user_id history rows and invalidates cache
        await storage.reset_user_data(user_id)
        assert await storage.get_user_play_history(user_id) == []
        assert storage._history_cache.get(user_id) is None

        # Other user's history is still preserved after user_id reset
        assert len(await storage.get_user_play_history(other_user)) == 1

        await storage.close()

    run_async(scenario())
