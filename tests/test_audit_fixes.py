import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.contracts import PlayerServices
from core.queue import QueueItem, TrackQueue
from core.registry import PlayerRegistry
from core.storage import Storage, StoredTrack
from tests.fakes import FakeBackend, FakeLoader, make_config, make_track
from utils.checks import require_dj
from utils.components_v2 import PaginatedPage, PaginatedView, reply_card
from utils.errors import DJRequired


def test_lazy_non_spotify_track_loading():
    async def scenario():
        cfg = make_config()
        backend = FakeBackend()
        loader = FakeLoader()
        registry = PlayerRegistry(PlayerServices(cfg, backend, loader))
        player = await registry.get_or_create(1, 10, 20)

        # Create item without a track object (e.g. from saved playlist or queue snapshot)
        item = QueueItem(
            track=None,
            title="Saved MP3",
            duration_ms=180000,
            requester_id=42,
            uri="https://example.com/audio.mp3",
            artist="Indie Artist",
            query="https://example.com/audio.mp3",
        )
        assert item.track is None

        # Enqueue should resolve the track via loader.load and play it
        res = await player.enqueue([item])
        assert res.started
        assert player.current is not None
        assert player.current.track is not None
        assert backend.audio(1).plays == 1

        await registry.destroy_all("test")

    asyncio.run(scenario())


def test_storage_rollback_on_failure(tmp_path):
    async def scenario():
        db_path = tmp_path / "rollback_test.db"
        storage = Storage(db_path)
        storage.start()

        # Normal operation works
        await storage.add_favorite(
            1,
            StoredTrack(uri="http://example.com/1", title="Track 1", artist="Artist 1", duration_ms=1000),
            limit=5,
        )
        assert len(await storage.get_favorites(1)) == 1

        # Submit an operation that causes an exception inside the worker thread
        def bad_action(conn):
            conn.execute("INSERT INTO nonexistent_table VALUES (1)")

        with pytest.raises(Exception):
            await storage._run(bad_action)

        # Connection should have rolled back cleanly and still be usable
        favs = await storage.get_favorites(1)
        assert len(favs) == 1

        await storage.close()

    asyncio.run(scenario())


def test_per_guild_dj_enforcement():
    cfg = make_config(DJ_ROLE_ID="999", OWNER_ID="100")
    bot = SimpleNamespace(cfg=cfg)

    admin = SimpleNamespace(
        id=1,
        guild_permissions=SimpleNamespace(administrator=True),
        roles=[],
    )
    owner = SimpleNamespace(
        id=100,
        guild_permissions=SimpleNamespace(administrator=False),
        roles=[],
    )
    dj_member = SimpleNamespace(
        id=2,
        guild_permissions=SimpleNamespace(administrator=False),
        roles=[SimpleNamespace(id=555)],
    )
    regular = SimpleNamespace(
        id=3,
        guild_permissions=SimpleNamespace(administrator=False),
        roles=[SimpleNamespace(id=777)],
    )

    # Admin and owner always pass (do not raise)
    require_dj(bot, cfg, admin, dj_role_id=555, dj_only=True)
    require_dj(bot, cfg, owner, dj_role_id=555, dj_only=True)

    # User with per-guild DJ role passes
    require_dj(bot, cfg, dj_member, dj_role_id=555, dj_only=True)

    # User without per-guild DJ role fails when dj_only is active
    with pytest.raises(DJRequired):
        require_dj(bot, cfg, regular, dj_role_id=555, dj_only=True)

    # When dj_role_id is not set per-guild (0), global cfg.dj_role_id is checked
    global_dj_member = SimpleNamespace(
        id=4,
        guild_permissions=SimpleNamespace(administrator=False),
        roles=[SimpleNamespace(id=999)],
    )
    require_dj(bot, cfg, global_dj_member, dj_role_id=0, dj_only=False)
    with pytest.raises(DJRequired):
        require_dj(bot, cfg, regular, dj_role_id=0, dj_only=False)


def test_concurrent_queue_iteration_safety():
    async def scenario():
        queue = TrackQueue(max_size=100, max_per_user=100)
        # Populate initial items
        for i in range(20):
            queue.add(QueueItem.from_track(make_track(i), 1))

        stop_event = asyncio.Event()

        async def reader():
            while not stop_event.is_set():
                # Iterating over queue should not raise RuntimeError: deque mutated
                _ = [item.title for item in queue]
                await asyncio.sleep(0.001)

        async def writer():
            counter = 100
            while not stop_event.is_set():
                counter += 1
                queue.push_front(QueueItem.from_track(make_track(counter), 1))
                if len(queue) > 30:
                    queue.remove_many(1, 1)
                await asyncio.sleep(0.001)

        readers = [asyncio.create_task(reader()) for _ in range(5)]
        writers = [asyncio.create_task(writer()) for _ in range(3)]

        await asyncio.sleep(0.1)
        stop_event.set()
        await asyncio.gather(*readers, *writers)

    asyncio.run(scenario())


def test_orphaned_card_cleanup_on_in_flight_destroy():
    async def scenario():
        cfg = make_config()
        backend = FakeBackend()
        loader = FakeLoader()
        registry = PlayerRegistry(PlayerServices(cfg, backend, loader))
        player = await registry.get_or_create(1, 10, 20)
        player.current = QueueItem.from_track(make_track(1), 1)

        # Intercept send_nowplaying_card to destroy the player before returning message id
        original_send = backend.send_nowplaying_card

        async def slow_send(channel_id, container, view):
            # Simulate player being destroyed while send is in-flight
            player.destroyed = True
            msg_id = await original_send(channel_id, container, view)
            return msg_id

        backend.send_nowplaying_card = slow_send

        # Updating card should detect player.destroyed and immediately delete the orphaned card
        await player._update_card_locked(recreate=True)

        # The message should have been deleted immediately
        assert len(backend.cards_deleted) == 1
        assert player.nowplaying_message_id is None

        await registry.destroy_all("test")

    asyncio.run(scenario())


def test_reply_card_sets_message_for_paginated_view():
    async def scenario():
        page = PaginatedPage(
            title="Page 1",
            items=["item 1", "item 2"],
            current_page=1,
            total_pages=2,
        )
        view = PaginatedView(items_provider=lambda p: page)
        mock_msg = MagicMock()
        mock_msg.edit = AsyncMock()

        interaction = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.original_response = AsyncMock(return_value=mock_msg)

        sent = await reply_card(interaction, view, ephemeral=False)
        assert sent is True
        assert view.message is mock_msg

        # Test on_timeout edits the message
        await view.on_timeout()
        mock_msg.edit.assert_awaited_once()

    asyncio.run(scenario())


def test_bot_activity_configured():
    import discord
    from core.alerts import Alerter
    from main import MusicBot

    cfg = make_config()
    bot = MusicBot(cfg, Alerter(cfg))
    assert bot.activity is not None
    assert bot.activity.type == discord.ActivityType.listening
    assert bot.activity.name == "/help"


def test_bot_channel_permissions_checks():
    import discord
    from discord import app_commands
    from utils.checks import check_bot_channel_permissions

    def make_interaction(voice_perms_dict=None, text_perms_dict=None):
        v_dict = {"view_channel": True, "connect": True, "speak": True}
        if voice_perms_dict:
            v_dict.update(voice_perms_dict)
        t_dict = {"view_channel": True, "send_messages": True, "embed_links": True, "attach_files": True}
        if text_perms_dict:
            t_dict.update(text_perms_dict)

        v_perms = SimpleNamespace(**v_dict)
        t_perms = SimpleNamespace(**t_dict)

        v_channel = SimpleNamespace(permissions_for=lambda m: v_perms)
        t_channel = SimpleNamespace(permissions_for=lambda m: t_perms)

        bot_member = MagicMock(spec=discord.Member)
        guild = SimpleNamespace(me=bot_member)
        interaction = SimpleNamespace(guild=guild, channel=t_channel)
        return interaction, v_channel

    # 1. All permissions present -> passes cleanly
    inter, v_ch = make_interaction()
    check_bot_channel_permissions(inter, voice_channel=v_ch)

    # 2. Missing voice speak permission -> raises BotMissingPermissions with "speak"
    inter, v_ch = make_interaction(voice_perms_dict={"speak": False})
    with pytest.raises(app_commands.BotMissingPermissions) as exc_info:
        check_bot_channel_permissions(inter, voice_channel=v_ch)
    assert "speak" in exc_info.value.missing_permissions

    # 3. Missing text embed_links and attach_files -> raises BotMissingPermissions
    inter, v_ch = make_interaction(text_perms_dict={"embed_links": False, "attach_files": False})
    with pytest.raises(app_commands.BotMissingPermissions) as exc_info:
        check_bot_channel_permissions(inter, voice_channel=v_ch)
    assert "embed_links" in exc_info.value.missing_permissions
    assert "attach_files" in exc_info.value.missing_permissions


def test_smooth_playback_and_speed_controls():
    async def scenario():
        cfg = make_config()
        backend = FakeBackend()
        loader = FakeLoader()
        registry = PlayerRegistry(PlayerServices(cfg, backend, loader))
        player = await registry.get_or_create(1, 10, 20)

        audio = backend.audio(1)
        assert audio is not None

        # 1. Preprocess audio applies smooth playback timescale by default (speed 0.98)
        await player._preprocess_audio_locked(audio)
        assert hasattr(audio, "filters")
        assert "timescale" in audio.filters
        assert audio.filters["timescale"].values["speed"] == 0.98

        # 2. Custom speed setting
        await player.set_speed(0.95)
        assert "speed" in player.applied_filters
        assert audio.filters["timescale"].values["speed"] == 0.95

        # 3. Reset speed back to 1.0 removes timescale filter
        await player.set_speed(1.0)
        assert "speed" not in player.applied_filters
        assert "timescale" not in audio.filters

        await registry.destroy_all("test")

    asyncio.run(scenario())


def test_preload_next_track_in_queue():
    async def scenario():
        cfg = make_config()
        backend = FakeBackend()
        loader = FakeLoader()
        registry = PlayerRegistry(PlayerServices(cfg, backend, loader))
        player = await registry.get_or_create(1, 10, 20)

        # Track 1 starts playing
        t1 = QueueItem.from_track(make_track(1), 1)
        # Track 2 is enqueued without audio track object
        t2 = QueueItem(
            track=None,
            title="Next Track",
            duration_ms=180000,
            requester_id=1,
            uri="https://example.com/next.mp3",
            query="https://example.com/next.mp3",
        )
        assert t2.track is None

        await player.enqueue([t1, t2])
        # Background preloading should resolve t2
        await asyncio.sleep(0.05)
        assert t2.track is not None

        await registry.destroy_all("test")

    asyncio.run(scenario())


def test_spotify_service_caching():
    async def scenario():
        from core.spotify import SpotifyService, SpotifyTrack

        mock_primary = MagicMock()
        mock_track = SpotifyTrack(title="Cached Track", artists=["Artist"], duration_ms=180000, uri="spotify:track:123")
        mock_primary.get_track = AsyncMock(return_value=mock_track)

        service = SpotifyService(enabled=True, primary=mock_primary)

        # First call fetches from primary
        res1 = await service.get_track("123")
        assert res1 == mock_track
        assert mock_primary.get_track.await_count == 1

        # Second call returns from cache without calling primary again
        res2 = await service.get_track("123")
        assert res2 == mock_track
        assert mock_primary.get_track.await_count == 1

    asyncio.run(scenario())


def test_rate_limit_error_handling():
    async def scenario():
        import discord
        from discord import app_commands
        from utils.errors import handle_app_command_error

        interaction = MagicMock(spec=discord.Interaction)
        interaction.is_expired.return_value = False
        interaction.command = SimpleNamespace(qualified_name="play")
        interaction.response = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()

        # Test CommandOnCooldown
        cooldown_err = app_commands.CommandOnCooldown(app_commands.Cooldown(1.0, 5.0), 4.2)
        await handle_app_command_error(interaction, cooldown_err)
        interaction.response.send_message.assert_awaited()
        call_msg = interaction.response.send_message.call_args.kwargs.get("content", "")
        assert "Rate limited" in call_msg or "On cooldown" in call_msg
        assert "5s" in call_msg

    asyncio.run(scenario())

