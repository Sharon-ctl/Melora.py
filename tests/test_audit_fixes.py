import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

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
    assert bot.activity.name == "/play"


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


def test_verify_phase8_non_ascii_rules(tmp_path):
    from scripts.verify_phase8 import scan_non_ascii

    # 1. autocomplete.py containing chr(0x1F50E) and chr(0x1F55B) is accepted
    auto_file = tmp_path / "autocomplete.py"
    auto_file.write_text(f"SEARCH = '{chr(0x1F50E)}'\nHISTORY = '{chr(0x1F55B)}'\nBULLET = '\u2022'", encoding="utf-8")
    assert scan_non_ascii([auto_file]) == []

    # 2. autocomplete.py containing any other emoji or non-ASCII is rejected
    auto_bad = tmp_path / "autocomplete.py"
    auto_bad.write_text(f"SEARCH = '{chr(0x1F50E)}'\nBAD = '{chr(0x1F3B5)}'", encoding="utf-8")
    hits = scan_non_ascii([auto_bad])
    assert len(hits) == 1
    assert "Non-ASCII" in hits[0]

    # 3. Another module containing chr(0x1F50E) or chr(0x1F55B) is rejected
    other_file = tmp_path / "other.py"
    other_file.write_text(f"SEARCH = '{chr(0x1F50E)}'", encoding="utf-8")
    hits = scan_non_ascii([other_file])
    assert len(hits) == 1
    assert "Non-ASCII" in hits[0]

    other_file2 = tmp_path / "other2.py"
    other_file2.write_text(f"HISTORY = '{chr(0x1F55B)}'", encoding="utf-8")
    hits2 = scan_non_ascii([other_file2])
    assert len(hits2) == 1
    assert "Non-ASCII" in hits2[0]

    # 4. Another module containing bullet is accepted
    bullet_file = tmp_path / "clean.py"
    bullet_file.write_text("BULLET = '\u2022'\nASCII = 'abc'", encoding="utf-8")
    assert scan_non_ascii([bullet_file]) == []


def test_reply_multiline_uses_container():
    async def scenario():
        import discord
        from discord.ui import Container
        from utils.components_v2 import BaseCardView
        from utils.interaction import reply

        interaction = MagicMock(spec=discord.Interaction)
        interaction.is_expired.return_value = False
        interaction.response = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.user = MagicMock()
        interaction.user.id = 12345
        interaction.guild_id = 67890

        # Multi-line content must be wrapped in BaseCardView with a Container
        await reply(interaction, "Line 1\nLine 2\nLine 3", ephemeral=True)
        interaction.response.send_message.assert_awaited_once()
        _, kwargs = interaction.response.send_message.call_args
        view = kwargs.get("view")
        assert isinstance(view, BaseCardView)
        assert any(isinstance(item, Container) for item in view.children)
        assert kwargs.get("ephemeral") is True
        assert "content" not in kwargs

        # Single-line content must be sent normally without card view
        interaction.response.send_message.reset_mock()
        await reply(interaction, "Single line message", ephemeral=False)
        interaction.response.send_message.assert_awaited_once()
        _, kwargs = interaction.response.send_message.call_args
        assert kwargs.get("content") == "Single line message"
        assert kwargs.get("view") is None

    asyncio.run(scenario())


def test_settings_view_uses_container():
    async def scenario():
        import discord
        from discord.ui import Container
        from cogs.settings import Settings
        from core.alerts import Alerter
        from main import MusicBot
        from utils.components_v2 import BaseCardView

        cfg = make_config()
        bot = MusicBot(cfg, Alerter(cfg))
        cog = Settings(bot)

        interaction = MagicMock(spec=discord.Interaction)
        interaction.guild_id = 999
        interaction.extras = {}
        interaction.is_expired.return_value = False
        member = MagicMock(spec=discord.Member)
        member.id = 111
        member.guild_permissions = discord.Permissions(manage_guild=True)
        interaction.user = member
        interaction.response = MagicMock()
        interaction.response.is_done.return_value = False

        async def fake_defer(*args, **kwargs):
            interaction.response.is_done.return_value = True

        interaction.response.defer = AsyncMock(side_effect=fake_defer)
        interaction.response.send_message = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        # Mock storage get_guild_settings
        from core.storage import GuildSettings
        bot.storage = MagicMock()
        bot.storage.get_guild_settings = AsyncMock(return_value=GuildSettings(guild_id=999))

        await cog.settings_view.callback(cog, interaction)

        # safe_defer was called, then reply_card edited original response with container view
        assert interaction.edit_original_response.called or interaction.response.send_message.called
        call_args = interaction.edit_original_response.call_args or interaction.response.send_message.call_args
        view = call_args.kwargs.get("view")
        assert isinstance(view, BaseCardView)
        assert any(isinstance(item, Container) for item in view.children)

    asyncio.run(scenario())


def test_status_loop_rotation():
    async def scenario():
        import discord
        from core.alerts import Alerter
        from main import MusicBot

        cfg = make_config()
        bot = MusicBot(cfg, Alerter(cfg))
        bot.wait_until_ready = AsyncMock()
        bot.change_presence = AsyncMock()

        # Simulate 2 iterations of _status_loop
        sleep_calls = []

        async def fake_sleep(secs):
            sleep_calls.append(secs)
            if len(sleep_calls) >= 2:
                # Stop the loop after 2 steps
                bot._shutdown_started = True
                bot.is_closed = lambda: True

        with patch.object(MusicBot, "guilds", new_callable=PropertyMock) as mock_guilds:
            mock_guilds.return_value = [MagicMock(), MagicMock(), MagicMock()]
            with patch("asyncio.sleep", side_effect=fake_sleep):
                await bot._status_loop()

        assert bot.change_presence.call_count == 2
        first_call = bot.change_presence.call_args_list[0].kwargs.get("activity")
        assert first_call.name == "in 3 servers"
        assert first_call.type == discord.ActivityType.listening

        second_call = bot.change_presence.call_args_list[1].kwargs.get("activity")
        assert second_call.name == "/play"
        assert second_call.type == discord.ActivityType.listening
        assert sleep_calls == [10.0, 10.0]

        # Test singular "1 server"
        bot.is_closed = lambda: False
        bot._shutdown_started = False
        sleep_calls.clear()
        bot.change_presence.reset_mock()
        with patch.object(MusicBot, "guilds", new_callable=PropertyMock) as mock_guilds:
            mock_guilds.return_value = [MagicMock()]
            with patch("asyncio.sleep", side_effect=fake_sleep):
                await bot._status_loop()

        singular_call = bot.change_presence.call_args_list[0].kwargs.get("activity")
        assert singular_call.name == "in 1 server"

    asyncio.run(scenario())


def test_in_memory_commands_acceleration_no_defer():
    """Verify in-memory playback and settings commands do not call defer, responding in 1 roundtrip."""
    async def scenario():
        import discord
        from cogs.music import Music
        from cogs.settings import Settings
        from core.alerts import Alerter
        from main import MusicBot

        def make_inter():
            inter = MagicMock(spec=discord.Interaction)
            inter.guild_id = 999
            inter.extras = {}
            inter.is_expired.return_value = False
            inter.response = MagicMock()
            inter.response.is_done.return_value = False
            inter.response.defer = AsyncMock()
            inter.response.send_message = AsyncMock()
            member = MagicMock(spec=discord.Member)
            member.id = 111
            member.guild_permissions = discord.Permissions(manage_guild=True)
            inter.user = member
            return inter

        cfg = make_config()
        bot = MusicBot(cfg, Alerter(cfg))
        music_cog = Music(bot)
        settings_cog = Settings(bot)

        # Setup mock player
        mock_player = MagicMock()
        mock_player.pause = AsyncMock()
        mock_player.resume = AsyncMock()
        mock_player.stop = AsyncMock()
        mock_player.previous = AsyncMock(return_value=MagicMock(title="Song 1"))
        mock_player.set_volume = AsyncMock(return_value=50)

        # 1. Test pause
        inter = make_inter()
        with (
            patch.object(music_cog, "_control_gate", return_value=mock_player),
            patch("cogs.music.reply", new_callable=AsyncMock) as mock_reply,
        ):
            await music_cog.pause.callback(music_cog, inter)
            assert not inter.response.defer.called
            mock_player.pause.assert_called_once()
            mock_reply.assert_called_once()

        # 2. Test resume
        inter = make_inter()
        with (
            patch.object(music_cog, "_control_gate", return_value=mock_player),
            patch("cogs.music.reply", new_callable=AsyncMock) as mock_reply,
        ):
            await music_cog.resume.callback(music_cog, inter)
            assert not inter.response.defer.called
            mock_player.resume.assert_called_once()
            mock_reply.assert_called_once()

        # 3. Test settings_view responds without deferring
        inter = make_inter()
        bot.storage = MagicMock()
        from core.storage import GuildSettings
        bot.storage.get_guild_settings = AsyncMock(return_value=GuildSettings(guild_id=999))
        with (
            patch.object(settings_cog, "_require_manage_guild", return_value=None),
            patch("cogs.settings.reply_card", new_callable=AsyncMock) as mock_reply_card,
        ):
            await settings_cog.settings_view.callback(settings_cog, inter)
            assert not inter.response.defer.called
            mock_reply_card.assert_called_once()

    asyncio.run(scenario())


def test_graceful_shutdown_sets_offline_and_saves_queues():
    async def scenario():
        import discord
        from discord.ext import commands
        from core.alerts import Alerter
        from main import MusicBot

        cfg = make_config()
        bot = MusicBot(cfg, Alerter(cfg))
        bot.is_closed = MagicMock(return_value=False)
        bot.change_presence = AsyncMock()
        bot.supervisor.stop_all = AsyncMock()
        bot.registry = MagicMock()
        bot.registry.stop_background_tasks = MagicMock()
        bot.registry.destroy_all = AsyncMock()
        bot.storage = MagicMock()
        bot.storage.close = AsyncMock()
        bot.lavalink = MagicMock()
        bot.lavalink.close = AsyncMock()
        bot.spotify = MagicMock()
        bot.spotify.close = AsyncMock()
        bot.alerter.close = AsyncMock()

        with patch.object(commands.AutoShardedBot, "close", new_callable=AsyncMock) as mock_super_close:
            await bot.close()

            # Verify presence set to offline
            bot.change_presence.assert_awaited_once_with(status=discord.Status.offline)
            # Verify supervisor stopped
            bot.supervisor.stop_all.assert_awaited_once()
            # Verify players destroyed with shutdown reason (saving queues)
            bot.registry.destroy_all.assert_awaited_once_with("shutdown")
            # Verify storage closed
            bot.storage.close.assert_awaited_once()
            # Verify super().close() called
            mock_super_close.assert_awaited_once()

    asyncio.run(scenario())


def test_graceful_shutdown_cancellation_resilient():
    async def scenario():
        from discord.ext import commands
        from core.alerts import Alerter
        from main import MusicBot

        cfg = make_config()
        bot = MusicBot(cfg, Alerter(cfg))
        bot.is_closed = MagicMock(return_value=False)
        bot.change_presence = AsyncMock()
        bot.supervisor.stop_all = AsyncMock(side_effect=asyncio.CancelledError())
        bot.registry = MagicMock()
        bot.registry.stop_background_tasks = MagicMock()
        bot.registry.destroy_all = AsyncMock()
        bot.storage = MagicMock()
        bot.storage.close = AsyncMock()
        bot.lavalink = MagicMock()
        bot.lavalink.close = AsyncMock()
        bot.alerter.close = AsyncMock()

        with patch.object(commands.AutoShardedBot, "close", new_callable=AsyncMock) as mock_super_close:
            # Even if a step experiences CancelledError, all subsequent steps must complete!
            await bot.close()
            bot.registry.destroy_all.assert_awaited_once_with("shutdown")
            bot.storage.close.assert_awaited_once()
            mock_super_close.assert_awaited_once()

    asyncio.run(scenario())


def test_player_save_snapshot_now():
    async def scenario():
        from core.guild_player import GuildPlayer
        from core.queue import QueueItem
        from tests.fakes import FakeBackend, FakeLoader

        cfg = make_config()
        backend = FakeBackend()
        loader = FakeLoader()
        storage = MagicMock()
        storage.save_queue_snapshot = AsyncMock()
        services = PlayerServices(cfg, backend, loader, storage)

        player = GuildPlayer(12345, 100, 200, services)
        player.current = QueueItem(track=None, title="Current Song", artist="Artist 1", duration_ms=180000, requester_id=999, uri="https://youtube.com/watch?v=curr")
        player.queue.add(QueueItem(track=None, title="Queued Song", artist="Artist 2", duration_ms=200000, requester_id=888, uri="https://youtube.com/watch?v=next"))

        await player.save_snapshot_now()

        storage.save_queue_snapshot.assert_awaited_once()
        guild_id, tracks = storage.save_queue_snapshot.await_args[0]
        assert guild_id == 12345
        assert len(tracks) == 2
        assert tracks[0].title == "Current Song"
        assert tracks[1].title == "Queued Song"

    asyncio.run(scenario())




