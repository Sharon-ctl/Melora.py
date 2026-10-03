"""Unit tests for Phase 4: Library, Settings, 24/7, and Queue Restore."""
from __future__ import annotations

from pathlib import Path
import pytest

from core.contracts import PlayerServices
from core.guild_player import GuildPlayer
from core.storage import Storage, StorageError, StoredTrack
from tests.fakes import FakeBackend, FakeLoader, make_config


@pytest.fixture
async def storage(tmp_path: Path):
    db_path = tmp_path / "test.db"
    store = Storage(db_path)
    store.start()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def services(storage: Storage) -> PlayerServices:
    cfg = make_config()
    backend = FakeBackend()
    loader = FakeLoader()
    return PlayerServices(backend=backend, loader=loader, cfg=cfg, storage=storage)


@pytest.mark.anyio
async def test_favorites_crud(storage: Storage):
    user_id = 12345
    t1 = StoredTrack(uri="https://youtu.be/1", title="Track 1", artist="Artist 1", duration_ms=180000)
    t2 = StoredTrack(uri="https://youtu.be/2", title="Track 2", artist="Artist 2", duration_ms=210000)

    # Initially empty
    favs = await storage.get_favorites(user_id)
    assert favs == []

    # Add 2 tracks
    p1 = await storage.add_favorite(user_id, t1, limit=5)
    p2 = await storage.add_favorite(user_id, t2, limit=5)
    assert p1 == 1
    assert p2 == 2

    # Check list
    favs = await storage.get_favorites(user_id)
    assert len(favs) == 2
    assert favs[0].title == "Track 1"
    assert favs[1].title == "Track 2"

    # Limit enforcement
    with pytest.raises(StorageError, match="Favorite limit of 2 reached."):
        await storage.add_favorite(user_id, t1, limit=2)

    # Remove position 1
    removed = await storage.remove_favorite(user_id, 1)
    assert removed.title == "Track 1"

    favs = await storage.get_favorites(user_id)
    assert len(favs) == 1
    assert favs[0].title == "Track 2"

    # Clear
    cleared = await storage.clear_favorites(user_id)
    assert cleared == 1
    assert await storage.get_favorites(user_id) == []


@pytest.mark.anyio
async def test_playlists_crud(storage: Storage):
    user_id = 54321
    # Create playlist
    pid = await storage.create_playlist(user_id, "Chill Vibes", limit=3)
    assert pid > 0

    # Duplicate name fails
    with pytest.raises(StorageError, match="already exists"):
        await storage.create_playlist(user_id, "Chill Vibes", limit=3)

    # List playlists
    pls = await storage.list_playlists(user_id)
    assert pls == ["Chill Vibes"]

    # Add tracks
    t1 = StoredTrack(uri="https://youtu.be/1", title="Track 1", artist="Artist 1", duration_ms=180000)
    t2 = StoredTrack(uri="https://youtu.be/2", title="Track 2", artist="Artist 2", duration_ms=210000)
    pos1 = await storage.add_playlist_track(user_id, "Chill Vibes", t1, limit=2)
    pos2 = await storage.add_playlist_track(user_id, "Chill Vibes", t2, limit=2)
    assert pos1 == 1
    assert pos2 == 2

    # Playlist track limit
    with pytest.raises(StorageError, match="Playlist track limit of 2 reached."):
        await storage.add_playlist_track(user_id, "Chill Vibes", t1, limit=2)

    # Get tracks
    tracks = await storage.get_playlist_tracks(user_id, "Chill Vibes")
    assert len(tracks) == 2
    assert tracks[0].title == "Track 1"

    # Rename
    renamed = await storage.rename_playlist(user_id, "Chill Vibes", "Study Beats")
    assert renamed is True
    assert await storage.list_playlists(user_id) == ["Study Beats"]

    # Remove track
    rem = await storage.remove_playlist_track(user_id, "Study Beats", 1)
    assert rem.title == "Track 1"
    tracks_after = await storage.get_playlist_tracks(user_id, "Study Beats")
    assert len(tracks_after) == 1
    assert tracks_after[0].title == "Track 2"

    # Delete playlist
    deleted = await storage.delete_playlist(user_id, "Study Beats")
    assert deleted is True
    assert await storage.list_playlists(user_id) == []


@pytest.mark.anyio
async def test_guild_settings_and_queue_snapshot(storage: Storage):
    guild_id = 9999

    # Default settings
    settings = await storage.get_guild_settings(guild_id)
    assert settings.guild_id == guild_id
    assert settings.default_volume == 100
    assert settings.voice_247_channel_id == 0
    assert settings.restore_queue is False

    # Update settings
    await storage.update_guild_settings(
        guild_id,
        voice_247_channel_id=1234,
        dj_role_id=5678,
        dj_only=True,
        volume_limit=80,
        restore_queue=True,
    )

    updated = await storage.get_guild_settings(guild_id)
    assert updated.voice_247_channel_id == 1234
    assert updated.dj_role_id == 5678
    assert updated.dj_only is True
    assert updated.volume_limit == 80
    assert updated.restore_queue is True

    # Queue snapshot save and load
    tracks = [
        StoredTrack(uri="https://youtu.be/a", title="Song A", artist="Band A", duration_ms=200000, requester_id=111),
        StoredTrack(uri="https://youtu.be/b", title="Song B", artist="Band B", duration_ms=250000, requester_id=222),
    ]
    await storage.save_queue_snapshot(guild_id, tracks)

    loaded = await storage.get_queue_snapshot(guild_id)
    assert len(loaded) == 2
    assert loaded[0].title == "Song A"
    assert loaded[0].requester_id == 111
    assert loaded[1].title == "Song B"

    # Delete snapshot
    await storage.delete_queue_snapshot(guild_id)
    assert await storage.get_queue_snapshot(guild_id) == []


@pytest.mark.anyio
async def test_player_247_suppresses_idle(services: PlayerServices):
    player = GuildPlayer(1, 10, 20, services)
    player.is_247 = True

    # Starting idle timer in 24/7 mode is a no-op
    player.start_timer("idle", 60.0, "idle")
    assert not player.has_timer("idle")

    # Starting alone timer in 24/7 mode is a no-op
    player.start_timer("alone", 60.0, "alone")
    assert not player.has_timer("alone")

    # Sleep timer is still allowed in 24/7 mode
    player.start_timer("sleep", 60.0, "sleep")
    assert player.has_timer("sleep")

    player.shutdown()
