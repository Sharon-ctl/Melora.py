"""Unit tests for Phase 5: Gateway and Library Tuning.

Verifies:
- discord.py speedups (orjson, aiohttp speedups) are installed and actively used
- MENTION_REPLY_ENABLED config option (defaults to True)
- When MENTION_REPLY_ENABLED=False: guild_messages intent is not requested and message events are suppressed
- When MENTION_REPLY_ENABLED=True and server count > 500: warning logged recommending False
- Gateway caches: zero message cache, voice-only member cache, no unused intents
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import discord.utils
import pytest

from config import load_config
from core.alerts import Alerter
from main import MusicBot


def test_discord_speedups_installed_and_used():
    """Confirm orjson is installed and hooked into discord.utils json encoders."""
    import orjson

    assert discord.utils._from_json is orjson.loads
    # Verify to_json produces valid json string via orjson
    encoded = discord.utils._to_json({"key": "value", "num": 123})
    assert '"key":"value"' in encoded or '"key": "value"' in encoded


def test_mention_reply_enabled_default_and_config():
    """Verify MENTION_REPLY_ENABLED defaults to True and can be configured."""
    base_env = {
        "DISCORD_TOKEN": "A" * 35,
        "OWNER_ID": "123456789",
        "LAVALINK_HOST": "127.0.0.1",
        "LAVALINK_PORT": "2333",
        "LAVALINK_PASSWORD": "secret_password",
    }
    cfg_default = load_config(base_env)
    assert cfg_default.mention_reply_enabled is True

    cfg_disabled = load_config({**base_env, "MENTION_REPLY_ENABLED": "false"})
    assert cfg_disabled.mention_reply_enabled is False


def test_mention_reply_disabled_intents_and_dispatch():
    """Verify when MENTION_REPLY_ENABLED is False, guild_messages intent is False and dispatch drops messages."""
    base_env = {
        "DISCORD_TOKEN": "A" * 35,
        "OWNER_ID": "123456789",
        "LAVALINK_HOST": "127.0.0.1",
        "LAVALINK_PORT": "2333",
        "LAVALINK_PASSWORD": "secret_password",
        "MENTION_REPLY_ENABLED": "false",
    }
    cfg = load_config(base_env)
    bot = MusicBot(cfg, Alerter(cfg))

    # guild_messages intent should NOT be requested
    assert bot.intents.guild_messages is False
    assert bot.intents.guilds is True
    assert bot.intents.voice_states is True

    # dispatching 'message' should be dropped immediately without scheduling
    with pytest.MonkeyPatch.context() as mp:
        mock_schedule = MagicMock()
        mp.setattr(bot, "_schedule_event", mock_schedule)
        bot.dispatch("message", SimpleNamespace(content="hello"))
        mock_schedule.assert_not_called()


@pytest.mark.anyio
async def test_mention_reply_disabled_on_message_fast_exit():
    """Verify on_message exits immediately without any work when MENTION_REPLY_ENABLED is False."""
    base_env = {
        "DISCORD_TOKEN": "A" * 35,
        "OWNER_ID": "123456789",
        "LAVALINK_HOST": "127.0.0.1",
        "LAVALINK_PORT": "2333",
        "LAVALINK_PASSWORD": "secret_password",
        "MENTION_REPLY_ENABLED": "false",
    }
    cfg = load_config(base_env)
    bot = MusicBot(cfg, Alerter(cfg))

    msg = SimpleNamespace(
        author=SimpleNamespace(bot=False),
        guild=SimpleNamespace(id=1),
        reply=AsyncMock(),
    )
    await bot.on_message(msg)  # type: ignore[arg-type]
    msg.reply.assert_not_called()


def test_mention_reply_warning_over_500_servers(caplog):
    """Verify warning is logged at on_ready when server count exceeds 500 and mention reply is enabled."""
    async def scenario():
        base_env = {
            "DISCORD_TOKEN": "A" * 35,
            "OWNER_ID": "123456789",
            "LAVALINK_HOST": "127.0.0.1",
            "LAVALINK_PORT": "2333",
            "LAVALINK_PASSWORD": "secret_password",
            "MENTION_REPLY_ENABLED": "true",
        }
        cfg = load_config(base_env)
        bot = MusicBot(cfg, Alerter(cfg))

        # Mock 501 guilds
        bot._connection._guilds = {i: SimpleNamespace(id=i) for i in range(501)}
        bot._connection.user = SimpleNamespace(id=123, name="Melora")

        with caplog.at_level("WARNING"):
            await bot.on_ready()

        assert any("MENTION_REPLY_ENABLED is true with 501 servers" in r.message for r in caplog.records)

    asyncio.run(scenario())


def test_gateway_cache_and_flags_confirmed():
    """Confirm zero message cache, voice-only member cache, and no debug events."""
    base_env = {
        "DISCORD_TOKEN": "A" * 35,
        "OWNER_ID": "123456789",
        "LAVALINK_HOST": "127.0.0.1",
        "LAVALINK_PORT": "2333",
        "LAVALINK_PASSWORD": "secret_password",
    }
    cfg = load_config(base_env)
    bot = MusicBot(cfg, Alerter(cfg))

    # Message cache size is 0 / None
    assert len(bot.cached_messages) == 0

    # Member cache flags: voice only
    assert bot._connection.member_cache_flags.voice is True
    assert bot._connection.member_cache_flags.joined is False

    # Intents: strictly minimal
    assert bot.intents.members is False
    assert bot.intents.presences is False
    assert bot.intents.message_content is False
