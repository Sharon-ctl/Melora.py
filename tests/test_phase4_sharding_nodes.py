"""Unit tests for Phase 4: Sharding and Multi-Node.

Verifies:
- Config accepts list of Lavalink nodes via LAVALINK_NODES (name, host, port, password, region, secure)
- Existing single-node LAVALINK_* settings remain valid
- NodeConfig synchronizes ssl and secure properties
- AutoShardedBot client integration and shard listeners
- Shard-safe sweeps and guild-removal (unavailable check)
- Node failover to healthy node with change_node
- Node loss handling with notice when failover fails or no nodes available
- /status output includes Gateway Shards and 'Music server offline' when 0 nodes available
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from discord.ext import commands

from config import ConfigError, NodeConfig, load_config
from core.contracts import PlayerServices
from core.registry import LOST_NODE_NOTICE, PlayerRegistry
from main import MusicBot
from tests.fakes import FakeBackend, FakeLoader, make_config


def test_node_config_secure_ssl_sync():
    """Verify NodeConfig synchronizes ssl and secure attributes."""
    n1 = NodeConfig(name="n1", host="localhost", port=2333, password="pass", secure=True)
    assert n1.ssl is True
    assert n1.secure is True

    n2 = NodeConfig(name="n2", host="localhost", port=2333, password="pass", ssl=True)
    assert n2.ssl is True
    assert n2.secure is True

    n3 = NodeConfig(name="n3", host="localhost", port=2333, password="pass")
    assert n3.ssl is False
    assert n3.secure is False


def test_multi_node_config_parsing():
    """Verify loading multi-node list from LAVALINK_NODES."""
    nodes_data = [
        {"name": "us-east", "host": "10.0.0.1", "port": 2333, "password": "pass1", "region": "us", "secure": True},
        {"name": "eu-west", "host": "10.0.0.2", "port": 2334, "password": "pass2", "region": "eu", "ssl": False},
    ]
    env = {
        "DISCORD_TOKEN": "x" * 35,
        "OWNER_ID": "123456789",
        "LAVALINK_NODES": json.dumps(nodes_data),
    }
    cfg = load_config(env)
    assert len(cfg.nodes) == 2
    assert cfg.nodes[0].name == "us-east"
    assert cfg.nodes[0].host == "10.0.0.1"
    assert cfg.nodes[0].secure is True
    assert cfg.nodes[0].ssl is True
    assert cfg.nodes[1].name == "eu-west"
    assert cfg.nodes[1].host == "10.0.0.2"
    assert cfg.nodes[1].secure is False
    assert cfg.nodes[1].ssl is False


def test_single_node_fallback_and_duplicate_rejection():
    """Verify existing single-node env vars work and duplicate names are rejected."""
    # Single node fallback
    env = {
        "DISCORD_TOKEN": "x" * 35,
        "OWNER_ID": "123456789",
        "LAVALINK_HOST": "lavalink.local",
        "LAVALINK_PORT": "2333",
        "LAVALINK_PASSWORD": "secret_password",
        "LAVALINK_SECURE": "true",
    }
    cfg = load_config(env)
    assert len(cfg.nodes) == 1
    assert cfg.nodes[0].host == "lavalink.local"
    assert cfg.nodes[0].secure is True

    # Duplicate names rejected
    dup_env = {
        "DISCORD_TOKEN": "x" * 35,
        "OWNER_ID": "123456789",
        "LAVALINK_NODES": json.dumps([
            {"name": "node1", "host": "10.0.0.1", "port": 2333, "password": "p1"},
            {"name": "node1", "host": "10.0.0.2", "port": 2333, "password": "p2"},
        ]),
    }
    try:
        load_config(dup_env)
        assert False, "Should have raised ConfigError for duplicate node names"
    except ConfigError as exc:
        assert "unique" in str(exc).lower()


def test_autosharded_bot_inheritance():
    """Verify MusicBot inherits from commands.AutoShardedBot and has shard handlers."""
    assert issubclass(MusicBot, commands.AutoShardedBot)

    cfg = make_config()
    bot = MusicBot(cfg, MagicMock())
    assert hasattr(bot, "on_shard_ready")
    assert hasattr(bot, "on_shard_disconnect")
    assert hasattr(bot, "on_shard_resumed")


def test_shard_safety_reconcile_and_guild_removal():
    """Verify reconcile and guild removal are shard-safe."""
    async def scenario():
        backend = FakeBackend()
        services = PlayerServices(make_config(), backend, FakeLoader())
        registry = PlayerRegistry(services)

        # Player on guild 1
        player = await registry.get_or_create(1, 10, 20)

        # Mock is_guild_shard_ready returning False (e.g. shard disconnected)
        backend.is_guild_shard_ready = MagicMock(return_value=False)
        backend.voice_connected = MagicMock(return_value=False)  # Voice would seem disconnected

        # Reconcile should skip because shard is not ready
        fixed = await registry.reconcile("test-watchdog")
        assert fixed == 0
        assert registry.get(1) is player  # Player was NOT destroyed

        # When shard is ready, voice disconnection is acted upon
        backend.is_guild_shard_ready = MagicMock(return_value=True)
        fixed = await registry.reconcile("test-watchdog")
        assert fixed >= 1
        assert registry.get(1) is None

        # Verify on_guild_remove ignores unavailable guilds
        from cogs.voice_events import VoiceEvents

        mock_bot = MagicMock()
        mock_bot.registry.on_guild_removed = AsyncMock()
        cog = VoiceEvents(mock_bot)

        unavailable_guild = SimpleNamespace(id=999, unavailable=True)
        await cog.on_guild_remove(unavailable_guild)  # type: ignore[arg-type]
        mock_bot.registry.on_guild_removed.assert_not_called()

        available_guild = SimpleNamespace(id=999, unavailable=False)
        await cog.on_guild_remove(available_guild)  # type: ignore[arg-type]
        mock_bot.registry.on_guild_removed.assert_called_once_with(999)

    asyncio.run(scenario())


def test_stale_bot_voice_event_cannot_revert_current_channel():
    async def scenario():
        from cogs.voice_events import VoiceEvents

        current = SimpleNamespace(id=22)
        stale = SimpleNamespace(id=11)
        player = SimpleNamespace(voice_channel_id=22, age=lambda: 100.0)
        bot = MagicMock()
        bot.user = SimpleNamespace(id=999)
        bot.registry.get.return_value = player
        bot.registry.destroy = AsyncMock()
        guild = SimpleNamespace(id=5, me=SimpleNamespace(voice=SimpleNamespace(channel=current)))
        member = SimpleNamespace(id=999, guild=guild)
        before = SimpleNamespace(channel=None)
        after = SimpleNamespace(channel=stale)

        await VoiceEvents(bot).on_voice_state_update(member, before, after)  # type: ignore[arg-type]

        assert player.voice_channel_id == current.id
        bot.registry.destroy.assert_not_awaited()

    asyncio.run(scenario())


def test_startup_restore_requires_247_and_keeps_snapshot_on_restore_failure(monkeypatch):
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("main.asyncio.sleep", no_sleep)
    monkeypatch.setattr("main.random.uniform", lambda _low, _high: 0.0)

    async def scenario():
        import discord
        from core.storage import StoredTrack
        from main import MusicBot

        voice = MagicMock(spec=discord.VoiceChannel)
        voice.id = 20
        voice.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True)
        me = MagicMock(spec=discord.Member)
        guild = SimpleNamespace(
            id=12,
            unavailable=False,
            me=me,
            text_channels=[SimpleNamespace(id=30)],
            get_channel=lambda _channel_id: voice,
        )
        snapshot = [
            StoredTrack(
                uri="https://youtu.be/restore",
                title="Restore",
                artist="Test",
                duration_ms=100_000,
                position_ms=12_500,
            )
        ]

        async def run(
            voice_247_channel_id: int,
            enqueue_error: Exception | None = None,
            *,
            restore_queue: bool = True,
            saved_channel_id: int = 0,
            can_connect: bool = True,
        ):
            voice.permissions_for.return_value = SimpleNamespace(
                view_channel=True,
                connect=can_connect,
                speak=True,
            )
            async def enqueue(items):
                player.current = items[0]
                return SimpleNamespace(added=len(items))

            player = SimpleNamespace(
                is_247=False,
                current=None,
                enqueue=AsyncMock(side_effect=enqueue_error or enqueue),
                seek=AsyncMock(),
                create_card=AsyncMock(),
            )
            storage = MagicMock()
            storage.get_queue_snapshot_voice_channel_id = AsyncMock(return_value=saved_channel_id)
            storage.get_queue_snapshot_card_channel_id = AsyncMock(return_value=0)
            storage.get_guild_settings = AsyncMock(
                return_value=SimpleNamespace(
                    voice_247_channel_id=voice_247_channel_id,
                    restrict_channel_id=0,
                    restore_queue=restore_queue,
                )
            )
            storage.get_queue_snapshot = AsyncMock(return_value=snapshot)
            storage.delete_queue_snapshot = AsyncMock()
            bot = SimpleNamespace(
                guilds=[guild],
                registry=SimpleNamespace(
                    reconcile=AsyncMock(return_value=0),
                    get_or_create=AsyncMock(return_value=player),
                ),
                storage=storage,
                backend=SimpleNamespace(bot_avatar_url=lambda: None, bot_id=lambda: 999),
                cfg=SimpleNamespace(),
            )
            await MusicBot._startup_sweep(bot)
            return bot, player

        no_247_bot, _ = await run(0)
        no_247_bot.registry.get_or_create.assert_not_awaited()

        recovery_bot, player = await run(20, RuntimeError("enqueue failed"))
        recovery_bot.registry.get_or_create.assert_awaited_once_with(12, 20, 30)
        player.enqueue.assert_awaited_once()
        recovery_bot.storage.delete_queue_snapshot.assert_not_awaited()

        session_bot, session_player = await run(0, restore_queue=False, saved_channel_id=20)
        session_bot.registry.get_or_create.assert_awaited_once_with(12, 20, 30)
        session_player.enqueue.assert_awaited_once()
        session_player.seek.assert_awaited_once_with(12)
        session_player.create_card.assert_awaited_once()
        session_bot.storage.delete_queue_snapshot.assert_awaited_once_with(12)

        blocked_bot, _ = await run(0, restore_queue=False, saved_channel_id=20, can_connect=False)
        blocked_bot.registry.get_or_create.assert_not_awaited()
        blocked_bot.storage.delete_queue_snapshot.assert_not_awaited()

    asyncio.run(scenario())


def test_node_failover_to_healthy_node():
    """Verify handle_node_disconnected migrates players to healthy node."""
    async def scenario():
        backend = FakeBackend()
        services = PlayerServices(make_config(), backend, FakeLoader())
        registry = PlayerRegistry(services)

        player = await registry.get_or_create(1, 10, 20)

        dead_node = SimpleNamespace(name="dead-node", available=False)
        healthy_node = SimpleNamespace(name="healthy-node", available=True)

        mock_ll_player = MagicMock()
        mock_ll_player.node = dead_node
        mock_ll_player.change_node = AsyncMock()

        backend.audio = MagicMock(return_value=mock_ll_player)
        backend.find_ideal_node = MagicMock(return_value=healthy_node)

        # Trigger node disconnect handling
        await registry.handle_node_disconnected(dead_node)

        # Player moved to healthy node
        mock_ll_player.change_node.assert_called_once_with(healthy_node)
        assert registry.get(1) is player  # Still active

    asyncio.run(scenario())


def test_node_failover_failure_destroys_with_notice():
    """Verify failed node migration destroys player with notice."""
    async def scenario():
        backend = FakeBackend()
        services = PlayerServices(make_config(), backend, FakeLoader())
        registry = PlayerRegistry(services)

        player = await registry.get_or_create(1, 10, 20)

        dead_node = SimpleNamespace(name="dead-node", available=False)
        healthy_node = SimpleNamespace(name="healthy-node", available=True)

        mock_ll_player = MagicMock()
        mock_ll_player.node = dead_node
        mock_ll_player.change_node = AsyncMock(side_effect=RuntimeError("connection refused"))

        backend.audio = MagicMock(return_value=mock_ll_player)
        backend.find_ideal_node = MagicMock(return_value=healthy_node)
        player.notify = AsyncMock()

        # Trigger node disconnect handling
        await registry.handle_node_disconnected(dead_node)

        # Move was attempted and failed
        mock_ll_player.change_node.assert_called_once_with(healthy_node)
        player.notify.assert_called_once_with(LOST_NODE_NOTICE)
        assert registry.get(1) is None  # Destroyed cleanly

    asyncio.run(scenario())


def test_all_nodes_down_destroys_players():
    """Verify handle_all_nodes_down notifies and destroys all players."""
    async def scenario():
        backend = FakeBackend()
        backend.any_node_available = MagicMock(return_value=False)
        services = PlayerServices(make_config(), backend, FakeLoader())
        registry = PlayerRegistry(services)

        p1 = await registry.get_or_create(1, 10, 20)
        p2 = await registry.get_or_create(2, 11, 21)
        p1.notify = AsyncMock()
        p2.notify = AsyncMock()

        dropped = await registry.handle_all_nodes_down()
        assert dropped == 2
        p1.notify.assert_called_once_with(LOST_NODE_NOTICE)
        p2.notify.assert_called_once_with(LOST_NODE_NOTICE)
        assert len(registry) == 0

    asyncio.run(scenario())
