"""Comprehensive unit tests for slash command autocomplete."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cogs.filters import Filters
from cogs.library import Library
from cogs.music import Music
from core.contracts import PlayerServices
from core.queue import QueueItem
from core.registry import PlayerRegistry
from core.storage import Storage, StoredTrack
from tests.fakes import FakeBackend, FakeLoader, make_config, make_track
from utils.autocomplete import HISTORY_PREFIX, SEARCH_PREFIX


def build_test_setup():
    cfg = make_config(AUTOCOMPLETE_SEARCH_ENABLED=True)
    backend = FakeBackend()
    loader = FakeLoader()
    storage = Storage(":memory:")
    storage.start()
    services = PlayerServices(cfg, backend, loader, storage)
    registry = PlayerRegistry(services)

    bot = SimpleNamespace(
        cfg=cfg,
        backend=backend,
        loader=loader,
        storage=storage,
        registry=registry,
    )
    return bot, registry, loader, storage


def fake_interaction(guild_id: int | None = 1, user_id: int = 42, **kwargs) -> SimpleNamespace:
    guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
    user = SimpleNamespace(id=user_id)
    namespace = SimpleNamespace(**kwargs)
    return SimpleNamespace(
        guild=guild,
        guild_id=guild_id,
        user=user,
        namespace=namespace,
    )


def test_queue_autocomplete_no_or_empty_player():
    async def scenario():
        bot, registry, _, _ = build_test_setup()
        music_cog = Music(bot)

        # 1. No player exists
        inter = fake_interaction(guild_id=100)
        choices = await music_cog._queue_pos_autocomplete(inter, current="")
        assert choices == []

        # 2. Player exists but queue is empty
        await registry.get_or_create(100, 10, 20)
        choices = await music_cog._queue_pos_autocomplete(inter, current="")
        assert choices == []

        # 3. Not in guild
        no_guild_inter = fake_interaction(guild_id=None)
        choices = await music_cog._queue_pos_autocomplete(no_guild_inter, current="")
        assert choices == []

        await registry.destroy_all("test")

    asyncio.run(scenario())


def test_queue_autocomplete_filtering_and_limits():
    async def scenario():
        bot, registry, _, _ = build_test_setup()
        music_cog = Music(bot)
        player = await registry.get_or_create(1, 10, 20)

        # Enqueue 30 tracks
        items = []
        for i in range(1, 31):
            artist = "Beatles" if i % 2 == 0 else "Queen"
            track = make_track(i, duration_ms=180_000, title=f"Melody Letter {chr(65 + (i % 26))}")
            setattr(track, "author", artist)
            item = QueueItem.from_track(track, requester_id=42)
            items.append(item)
        await player.enqueue(items)

        inter = fake_interaction(guild_id=1)

        # Max 25 choices when query is empty
        all_choices = await music_cog._queue_pos_autocomplete(inter, current="")
        assert len(all_choices) == 25
        assert all_choices[0].name.startswith("1. ")
        assert all_choices[0].value == 1

        # Filter by number prefix
        num_choices = await music_cog._queue_pos_autocomplete(inter, current="2")
        # Should match positions 2, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29
        assert all(str(c.value).startswith("2") for c in num_choices)

        # Filter by artist substring
        queen_choices = await music_cog._queue_pos_autocomplete(inter, current="queen")
        assert len(queen_choices) > 0
        assert all("queen" in c.name.lower() for c in queen_choices)

        # Filter by title substring
        letter_b_choices = await music_cog._queue_pos_autocomplete(inter, current="Letter B")
        assert len(letter_b_choices) >= 1
        assert "Letter B" in letter_b_choices[0].name

        await registry.destroy_all("test")

    asyncio.run(scenario())


def test_search_autocomplete():
    async def scenario():
        bot, _, loader, _ = build_test_setup()
        music_cog = Music(bot)
        inter = fake_interaction(guild_id=1, user_id=123)

        # Short query (< 3 chars) returns empty when user has no history
        assert await music_cog._search_autocomplete(inter, "ab") == []

        # URL query returns empty
        assert await music_cog._search_autocomplete(inter, "https://youtube.com/watch?v=123") == []

        # Valid query returns up to 10 results with search prefix
        results = await music_cog._search_autocomplete(inter, "bohemian rhapsody")
        assert len(results) == 10
        assert "bohemian rhapsody result 1" in results[0].name
        assert results[0].name == f"{SEARCH_PREFIX} {results[0].value}"
        assert SEARCH_PREFIX not in results[0].value

        # Cached on repeat
        cached = await music_cog._search_autocomplete(inter, "bohemian rhapsody")
        assert cached == results

        # Rate limiting on different query within 1s
        rate_limited = await music_cog._search_autocomplete(inter, "another one bites the dust")
        assert rate_limited == []

        # Disabled by config
        bot.cfg = make_config(AUTOCOMPLETE_SEARCH_ENABLED=False)
        assert await music_cog._search_autocomplete(inter, "testing disabled") == []

        # Node offline
        bot.cfg = make_config(AUTOCOMPLETE_SEARCH_ENABLED=True)
        loader.has_node = lambda: False
        assert await music_cog._search_autocomplete(inter, "testing offline") == []

    asyncio.run(scenario())


def test_history_autocomplete_empty_and_filtered():
    async def scenario():
        bot, _, _, storage = build_test_setup()
        music_cog = Music(bot)
        user_id = 42

        # 1. Empty history returns []
        inter_empty = fake_interaction(guild_id=1, user_id=user_id)
        assert await music_cog._search_autocomplete(inter_empty, "") == []
        assert await music_cog._search_autocomplete(inter_empty, "a") == []
        assert await music_cog._search_autocomplete(inter_empty, "ab") == []

        # 2. Populate 30 tracks for user_id with timestamps spaced out
        # Track 30 is newest, Track 1 is oldest
        for i in range(1, 31):
            artist = "Beatles" if i % 2 == 0 else "Queen"
            uri = f"https://example.com/track_{i}" if i != 20 else "https://example.com/" + ("x" * 120)
            await storage.record_user_play_history(
                user_id=user_id,
                title=f"Song Number {i} With Very Long Title " + ("A" * 60 if i == 6 else ""),
                artist=artist,
                uri=uri,
                max_entries=50,
            )
            await asyncio.sleep(0.002)

        # Populate a different user's history
        await storage.record_user_play_history(
            user_id=999,
            title="Secret Song By Another User",
            artist="Another Artist",
            uri="https://example.com/secret",
            max_entries=50,
        )

        # 3. Empty input "" shows user's own history, newest first, up to 25 entries
        inter = fake_interaction(guild_id=1, user_id=user_id)
        results = await music_cog._search_autocomplete(inter, "")
        assert len(results) == 25
        # Newest first: track 30 should be at index 0
        assert "Song Number 30" in results[0].name
        assert results[0].name.startswith(f"{HISTORY_PREFIX} ")
        assert len(results[0].name) <= 100
        # Value contains no emoji
        assert HISTORY_PREFIX not in results[0].value
        assert SEARCH_PREFIX not in results[0].value
        assert results[0].value == "https://example.com/track_30"

        # Check all 25 results
        for r in results:
            assert r.name.startswith(f"{HISTORY_PREFIX} ")
            assert len(r.name) <= 100
            assert HISTORY_PREFIX not in r.value
            assert SEARCH_PREFIX not in r.value
            # Never show user 999's tracks
            assert "Secret Song" not in r.name

        # Track with long URI (> 100 chars, track 20) uses Title Artist
        track_20_results = [r for r in results if "Song Number 20" in r.name]
        assert len(track_20_results) == 1
        assert not track_20_results[0].value.startswith("https://")
        assert "Song Number 20" in track_20_results[0].value
        assert len(track_20_results[0].value) <= 100

        # 4. 1 or 2 characters typed: filter history by title or artist (case-insensitive)
        queen_results = await music_cog._search_autocomplete(inter, "qu")
        assert len(queen_results) > 0
        assert all("Queen" in r.name for r in queen_results)
        assert all(r.name.startswith(f"{HISTORY_PREFIX} ") for r in queen_results)

        # Query "6" -> matches "Song Number 6", "Song Number 16", "Song Number 26"
        six_results = await music_cog._search_autocomplete(inter, "6")
        assert len(six_results) == 3
        # Check track 6 truncation <= 100 chars
        track_6 = [r for r in six_results if "Song Number 6" in r.name][0]
        assert len(track_6.name) <= 100

        # 1 or 2 characters with no match returns []
        no_match = await music_cog._search_autocomplete(inter, "zz")
        assert no_match == []

        # 5. Isolation: user 999 sees only their track
        inter_999 = fake_interaction(guild_id=1, user_id=999)
        results_999 = await music_cog._search_autocomplete(inter_999, "")
        assert len(results_999) == 1
        assert "Secret Song By Another User" in results_999[0].name
        assert results_999[0].value == "https://example.com/secret"

        await storage.close()

    asyncio.run(scenario())


def test_history_autocomplete_timeout_disabled_and_shedding():
    async def scenario():
        bot, _, _, storage = build_test_setup()
        music_cog = Music(bot)
        user_id = 77

        # Seed history
        await storage.record_user_play_history(
            user_id=user_id,
            title="Cached Track",
            artist="Artist",
            uri="https://example.com/cached",
            max_entries=50,
        )
        inter = fake_interaction(guild_id=1, user_id=user_id)

        # 1. History is cached. Now simulate load shedding on bot
        bot.is_load_shedding = True

        # Case < 3 chars: history is still returned from cache under load shedding!
        history_choices = await music_cog._search_autocomplete(inter, "")
        assert len(history_choices) == 1
        assert "Cached Track" in history_choices[0].name

        # Case >= 3 chars: external search suggestions are shed under load shedding!
        search_choices = await music_cog._search_autocomplete(inter, "cached")
        assert search_choices == []

        # 2. Disabled config: returns [] for cases 1 and 2
        bot.cfg = make_config(USER_HISTORY_ENABLED=False)
        assert await music_cog._search_autocomplete(inter, "") == []
        assert await music_cog._search_autocomplete(inter, "c") == []
        assert await music_cog._search_autocomplete(inter, "ca") == []

        # 3. Storage timeout (> 1.5s): cache miss that times out returns []
        bot.cfg = make_config(USER_HISTORY_ENABLED=True)
        storage._history_cache.invalidate(user_id)

        async def slow_get_history(uid, limit=50):
            await asyncio.sleep(2.0)
            return []

        storage.get_user_play_history = slow_get_history
        timeout_choices = await music_cog._search_autocomplete(inter, "")
        assert timeout_choices == []

        await storage.close()

    asyncio.run(scenario())


def test_library_autocomplete():
    async def scenario():
        bot, _, _, storage = build_test_setup()
        library_cog = Library(bot)
        user_id = 999
        inter = fake_interaction(guild_id=1, user_id=user_id)

        # 1. Favorites autocomplete
        track1 = StoredTrack(uri="http://a", title="Yesterday", artist="Beatles", duration_ms=120_000, requester_id=user_id)
        track2 = StoredTrack(uri="http://b", title="Help!", artist="Beatles", duration_ms=130_000, requester_id=user_id)
        await storage.add_favorite(user_id, track1, limit=50)
        await storage.add_favorite(user_id, track2, limit=50)

        fav_choices = await library_cog.favorites_remove_autocomplete(inter, current="")
        assert len(fav_choices) == 2
        assert fav_choices[0].value == 1
        assert "Yesterday" in fav_choices[0].name
        assert fav_choices[1].value == 2
        assert "Help!" in fav_choices[1].name

        # 2. Playlist name autocomplete
        await storage.create_playlist(user_id, "Rock Classics", limit=20)
        await storage.create_playlist(user_id, "Chill Vibes", limit=20)

        pl_choices = await library_cog._playlist_name_autocomplete(inter, current="rock")
        assert len(pl_choices) == 1
        assert pl_choices[0].name == "Rock Classics"
        assert pl_choices[0].value == "Rock Classics"

        # 3. Playlist track index autocomplete
        await storage.add_playlist_track(user_id, "Rock Classics", track1, limit=100)
        await storage.add_playlist_track(user_id, "Rock Classics", track2, limit=100)

        inter_with_ns = fake_interaction(guild_id=1, user_id=user_id, name="Rock Classics")
        track_choices = await library_cog.playlist_remove_index_autocomplete(inter_with_ns, current="")
        assert len(track_choices) == 2
        assert track_choices[0].value == 1
        assert "Yesterday" in track_choices[0].name

        # Missing playlist name in namespace
        inter_empty_ns = fake_interaction(guild_id=1, user_id=user_id, name="")
        assert await library_cog.playlist_remove_index_autocomplete(inter_empty_ns, current="") == []

    asyncio.run(scenario())


def test_presets_autocomplete():
    async def scenario():
        bot, registry, _, _ = build_test_setup()
        filters_cog = Filters(bot)
        filters_cog.eq_presets = {"bass_boost": [0] * 15, "treble_boost": [0] * 15, "vocal": [0] * 15}
        filters_cog.filter_presets = {"nightcore": {}, "vaporwave": {}, "karaoke": {}}

        inter = fake_interaction(guild_id=1)

        # No player -> empty choices
        assert await filters_cog.eq_preset_autocomplete(inter, "") == []
        assert await filters_cog.filter_add_autocomplete(inter, "") == []
        assert await filters_cog.filter_remove_autocomplete(inter, "") == []

        # Player exists
        player = await registry.get_or_create(1, 10, 20)

        # EQ autocomplete
        eq_choices = await filters_cog.eq_preset_autocomplete(inter, "boost")
        assert len(eq_choices) == 2
        assert {c.value for c in eq_choices} == {"bass_boost", "treble_boost"}

        # Filter add autocomplete
        filter_choices = await filters_cog.filter_add_autocomplete(inter, "core")
        assert len(filter_choices) == 1
        assert filter_choices[0].value == "nightcore"

        # Filter remove autocomplete
        player.applied_filters.add("nightcore")
        remove_choices = await filters_cog.filter_remove_autocomplete(inter, "")
        assert len(remove_choices) == 1
        assert remove_choices[0].value == "nightcore"

        await registry.destroy_all("test")

    asyncio.run(scenario())
