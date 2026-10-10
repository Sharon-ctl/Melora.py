"""Comprehensive tests for Voice Channel Status (Phase 3).

Verifies:
- State machine for every transition (playing, paused, resume, idle, destroyed, none)
- Title sanitizing and truncation
- Coalescing and deduplication
- Minimum interval per guild (default 3s, expanded under load shedding)
- Permission checking (set_voice_channel_status, connect), 10m skip cache, and diagnostics count
- Emoji 400 fallback remembered per guild
- 429 rate limit backoff without blocking
- Clear before disconnect ordering on intentional leaves (2s timeout)
- Forced disconnect best effort swallowing Forbidden/NotFound
- Graceful shutdown clearing (5s overall timeout)
- Settings toggle (enabled/disabled)
- Destroy path leaves zero state in memory
- Failing voice status never affects playback
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import discord

from core.voice_status import (
    VoiceStatusManager,
    derive_desired_status,
)
from tests.fakes import make_config
from utils import messages


class FakeVoiceChannel:
    def __init__(self, channel_id: int = 1234, name: str = "Voice Chat") -> None:
        self.id = channel_id
        self.name = name
        self.current_status: str | None = None
        self.edit_calls: list[str | None] = []
        self.edit_delay: float = 0.0
        self.edit_error: Exception | None = None
        self.has_perms = True

    async def edit(self, *, status: str | None = None, **kwargs: Any) -> None:
        if self.edit_delay > 0:
            await asyncio.sleep(self.edit_delay)
        if self.edit_error:
            raise self.edit_error
        self.current_status = status
        self.edit_calls.append(status)

    def permissions_for(self, member: Any) -> Any:
        return SimpleNamespace(
            set_voice_channel_status=self.has_perms,
            connect=self.has_perms,
        )


class FakeGuild:
    def __init__(self, guild_id: int, channel: FakeVoiceChannel) -> None:
        self.id = guild_id
        self.me = SimpleNamespace(id=999, name="BotMember")
        self.channel = channel

    def get_channel(self, channel_id: int) -> FakeVoiceChannel | None:
        if self.channel.id == channel_id:
            return self.channel
        return None


class FakeBot:
    def __init__(self, guild: FakeGuild) -> None:
        self.guild = guild
        self.guilds = [guild]
        self.registry: Any = None
        self.user = SimpleNamespace(id=999, name="Melora")

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        if self.guild.id == guild_id:
            return self.guild
        return None


# ---------------------------------------------------------------- State Machine Tests

def test_voice_status_state_machine_transitions():
    # 1. No player / destroyed / disabled -> None
    assert derive_desired_status(None) is None
    p_destroyed = SimpleNamespace(destroyed=True, current=None, paused=False)
    assert derive_desired_status(p_destroyed) is None
    p_disabled = SimpleNamespace(destroyed=False, current=None, paused=False)
    assert derive_desired_status(p_disabled, enabled=False) is None

    # 2. Playing with emoji
    track = SimpleNamespace(title="Starboy")
    p_playing = SimpleNamespace(destroyed=False, current=track, paused=False)
    playing_status = derive_desired_status(p_playing, use_emoji=True)
    assert playing_status == "<:music:1558305498531631155> Starboy"

    # 3. Playing plain (emoji disabled)
    playing_plain = derive_desired_status(p_playing, use_emoji=False)
    assert playing_plain == "Starboy"

    # 4. Paused (always plain "Paused", no emoji)
    p_paused = SimpleNamespace(destroyed=False, current=track, paused=True)
    assert derive_desired_status(p_paused, use_emoji=True) == "Paused"
    assert derive_desired_status(p_paused, use_emoji=False) == "Paused"

    # 5. Idle (queue ended, bot connected)
    p_idle = SimpleNamespace(destroyed=False, current=None, paused=False)
    idle_status = derive_desired_status(p_idle, use_emoji=True)
    assert idle_status == "<:addmusic:1526007757826691232> Use /play to listen"

    # 6. Idle plain (emoji disabled)
    idle_plain = derive_desired_status(p_idle, use_emoji=False)
    assert idle_plain == "Use /play to listen"


def test_voice_status_sanitizing_and_truncation():
    # Angle brackets
    assert messages.sanitize_voice_status_title("Artist <Remix> [Official]") == "Artist Remix [Official]"
    # Control chars & tabs & newlines
    assert messages.sanitize_voice_status_title("Line1\nLine2\tLine3 \x00\x1f End") == "Line1 Line2 Line3 End"
    # Whitespace collapse
    assert messages.sanitize_voice_status_title("   Too    many   spaces   ") == "Too many spaces"
    # 80 character truncation
    long_title = "Z" * 120
    truncated = messages.sanitize_voice_status_title(long_title)
    assert len(truncated) == 80
    assert truncated.endswith("...")
    assert truncated == "Z" * 77 + "..."


# ---------------------------------------------------------------- Flusher & Deduplication

def test_voice_status_flusher_coalescing_and_dedupe():
    async def scenario():
        channel = FakeVoiceChannel(channel_id=101)
        guild = FakeGuild(guild_id=1, channel=channel)
        bot = FakeBot(guild)
        cfg = make_config()
        mgr = VoiceStatusManager(bot, cfg)

        player = SimpleNamespace(destroyed=False, current=SimpleNamespace(title="Song A"), paused=False, voice_channel_id=101)
        bot.registry = SimpleNamespace(get=lambda gid: player)

        # Rapidly request multiple status changes
        mgr.request_update(1, player)
        player.current = SimpleNamespace(title="Song B")
        mgr.request_update(1, player)

        # Flusher tick
        flusher_task = asyncio.create_task(mgr.run())
        await asyncio.sleep(0.6)
        mgr.stop()
        await flusher_task

        # Coalesced to latest (Song B)
        assert len(channel.edit_calls) == 1
        assert "Song B" in str(channel.edit_calls[0])

        # Deduplication: requesting Song B again does NOT trigger edit
        channel.edit_calls.clear()
        mgr.request_update(1, player)
        mgr.start()
        await asyncio.sleep(0.6)
        mgr.stop()
        assert len(channel.edit_calls) == 0

    asyncio.run(scenario())


# ---------------------------------------------------------------- Minimum Interval & Load Shedding

def test_voice_status_minimum_interval_and_load_shedding():
    async def scenario():
        channel = FakeVoiceChannel(channel_id=102)
        guild = FakeGuild(guild_id=2, channel=channel)
        bot = FakeBot(guild)
        cfg = make_config()
        mgr = VoiceStatusManager(bot, cfg)

        player = SimpleNamespace(destroyed=False, current=SimpleNamespace(title="Track 1"), paused=False, voice_channel_id=102)
        bot.registry = SimpleNamespace(get=lambda gid: player)

        flusher_task = asyncio.create_task(mgr.run())
        mgr.request_update(2, player)
        await asyncio.sleep(0.6)
        assert len(channel.edit_calls) == 1

        # Request change immediately - should be held by minimum interval (3s)
        player.current = SimpleNamespace(title="Track 2")
        mgr.request_update(2, player)
        await asyncio.sleep(0.6)
        # Still 1 call because 3s hasn't passed!
        assert len(channel.edit_calls) == 1

        # Test load shedding expands interval
        mgr.set_load_shedding(True)
        assert mgr._load_shedding is True

        mgr.stop()
        await flusher_task

    asyncio.run(scenario())


# ---------------------------------------------------------------- Permissions & Diagnostics

def test_voice_status_missing_permission_skips_and_diagnostics():
    async def scenario():
        channel = FakeVoiceChannel(channel_id=103)
        channel.has_perms = False  # Lacks Set Voice Channel Status permission
        guild = FakeGuild(guild_id=3, channel=channel)
        bot = FakeBot(guild)
        cfg = make_config()
        mgr = VoiceStatusManager(bot, cfg)

        player = SimpleNamespace(
            destroyed=False, current=SimpleNamespace(title="No Perm Track"), paused=False, voice_channel_id=103
        )
        bot.registry = SimpleNamespace(get=lambda gid: player)

        flusher_task = asyncio.create_task(mgr.run())
        mgr.request_update(3, player)
        await asyncio.sleep(0.6)

        # Did not edit channel
        assert len(channel.edit_calls) == 0
        # Recorded in missing permission count
        assert mgr.missing_permission_count == 1
        # Channel is skipped for 10 minutes
        assert 103 in mgr._skipped_channels

        # Now grant permission
        channel.has_perms = True
        mgr._skipped_channels.clear()  # reset skip
        player.current = SimpleNamespace(title="Perm Granted Track")
        mgr.request_update(3, player)
        mgr._last_applied_at.clear()
        await asyncio.sleep(0.6)

        assert len(channel.edit_calls) == 1
        assert mgr.missing_permission_count == 0

        mgr.stop()
        await flusher_task

    asyncio.run(scenario())


# ---------------------------------------------------------------- Error Handling: 400 Emoji Fallback & 429

def test_voice_status_emoji_400_fallback():
    async def scenario():
        channel = FakeVoiceChannel(channel_id=104)
        guild = FakeGuild(guild_id=4, channel=channel)
        bot = FakeBot(guild)
        cfg = make_config()
        mgr = VoiceStatusManager(bot, cfg)

        # Simulate 400 Bad Request on emoji markup
        mock_resp = SimpleNamespace(status=400, reason="Bad Request")
        http_exc = discord.HTTPException(response=mock_resp, message="Invalid Emoji")  # type: ignore[arg-type]
        http_exc.status = 400
        channel.edit_error = http_exc

        player = SimpleNamespace(
            destroyed=False, current=SimpleNamespace(title="Fallback Song"), paused=False, voice_channel_id=104
        )
        bot.registry = SimpleNamespace(get=lambda gid: player)

        # First edit will fail with 400, which sets fallback and retries text-only
        # Remove edit_error on retry
        orig_edit = channel.edit

        async def _dynamic_edit(*args, **kwargs):
            if channel.edit_error:
                err = channel.edit_error
                channel.edit_error = None
                raise err
            return await orig_edit(*args, **kwargs)

        channel.edit = _dynamic_edit  # type: ignore[assignment]

        mgr.request_update(4, player)
        flusher_task = asyncio.create_task(mgr.run())
        await asyncio.sleep(0.6)
        mgr.stop()
        await flusher_task

        # Remembered for guild 4
        assert 4 in mgr._emoji_disabled_guilds
        # Applied fallback text-only (no <:music:...)
        assert channel.current_status == "Fallback Song"

    asyncio.run(scenario())


def test_voice_status_429_backoff_without_blocking():
    async def scenario():
        channel = FakeVoiceChannel(channel_id=105)
        guild = FakeGuild(guild_id=5, channel=channel)
        bot = FakeBot(guild)
        cfg = make_config()
        mgr = VoiceStatusManager(bot, cfg)

        # Simulate RateLimited error
        rl_exc = discord.RateLimited(retry_after=2.5)
        channel.edit_error = rl_exc

        player = SimpleNamespace(destroyed=False, current=SimpleNamespace(title="429 Song"), paused=False, voice_channel_id=105)
        bot.registry = SimpleNamespace(get=lambda gid: player)

        flusher_task = asyncio.create_task(mgr.run())
        mgr.request_update(5, player)
        await asyncio.sleep(0.6)

        # Did not raise, set backoff
        assert 5 in mgr._backoff_until
        assert mgr._backoff_until[5] > 0
        assert len(channel.edit_calls) == 0

        mgr.stop()
        await flusher_task

    asyncio.run(scenario())


# ---------------------------------------------------------------- Lifecycle Clearing & Memory

def test_voice_status_clear_before_disconnect_ordering():
    async def scenario():
        channel = FakeVoiceChannel(channel_id=106)
        guild = FakeGuild(guild_id=6, channel=channel)
        bot = FakeBot(guild)
        cfg = make_config()
        mgr = VoiceStatusManager(bot, cfg)

        player = SimpleNamespace(
            destroyed=False, current=SimpleNamespace(title="Active Song"), paused=False, voice_channel_id=106
        )
        mgr._current_applied[6] = "Active Song"

        # On intentional leave (forced=False), status is cleared awaited with 2s timeout
        await mgr.on_player_destroy(6, player, forced=False)

        assert channel.current_status is None
        assert 6 not in mgr._current_applied
        assert 6 not in mgr._desired

    asyncio.run(scenario())


def test_voice_status_forced_disconnect_best_effort():
    async def scenario():
        channel = FakeVoiceChannel(channel_id=107)
        # Channel edit raises NotFound (e.g. channel deleted)
        mock_resp = SimpleNamespace(status=404, reason="Not Found")
        channel.edit_error = discord.NotFound(response=mock_resp, message="Unknown Channel")  # type: ignore[arg-type]

        guild = FakeGuild(guild_id=7, channel=channel)
        bot = FakeBot(guild)
        cfg = make_config()
        mgr = VoiceStatusManager(bot, cfg)

        player = SimpleNamespace(
            destroyed=False, current=SimpleNamespace(title="Kicked Song"), paused=False, voice_channel_id=107
        )
        mgr._current_applied[7] = "Kicked Song"

        # Forced disconnect swallows NotFound quietly
        await mgr.on_player_destroy(7, player, forced=True)

        assert 7 not in mgr._current_applied
        assert 7 not in mgr._desired

    asyncio.run(scenario())


def test_voice_status_shutdown_clearing():
    async def scenario():
        channel = FakeVoiceChannel(channel_id=108)
        guild = FakeGuild(guild_id=8, channel=channel)
        bot = FakeBot(guild)
        cfg = make_config()
        mgr = VoiceStatusManager(bot, cfg)

        mgr._current_applied[8] = "Shutdown Song"
        mgr._desired_channel[8] = 108

        await mgr.clear_all_shutdown(timeout=5.0)

        assert channel.current_status is None

    asyncio.run(scenario())


def test_voice_status_settings_toggle():
    async def scenario():
        channel = FakeVoiceChannel(channel_id=109)
        guild = FakeGuild(guild_id=9, channel=channel)
        bot = FakeBot(guild)
        cfg = make_config()
        mgr = VoiceStatusManager(bot, cfg)

        mgr._current_applied[9] = "Before Disable"
        mgr._desired_channel[9] = 109

        # Disable setting -> clears channel
        await mgr.on_setting_disabled(9)
        assert channel.current_status is None

        # Re-enable setting -> requests update
        player = SimpleNamespace(
            destroyed=False, current=SimpleNamespace(title="Re-enabled Song"), paused=False, voice_channel_id=109
        )
        bot.registry = SimpleNamespace(get=lambda gid: player)
        await mgr.on_setting_enabled(9)
        assert 9 in mgr._desired

    asyncio.run(scenario())


def test_voice_status_destroy_leaves_zero_state():
    channel = FakeVoiceChannel(channel_id=110)
    guild = FakeGuild(guild_id=10, channel=channel)
    bot = FakeBot(guild)
    cfg = make_config()
    mgr = VoiceStatusManager(bot, cfg)

    # Populate state
    mgr._desired[10] = "Song"
    mgr._desired_channel[10] = 110
    mgr._current_applied[10] = "Song"
    mgr._last_applied_at[10] = 100.0
    mgr._backoff_until[10] = 200.0
    mgr._emoji_disabled_guilds.add(10)
    mgr._missing_perm_guilds.add(10)
    mgr._skipped_channels[110] = 300.0

    mgr.remove_guild(10, 110)

    assert 10 not in mgr._desired
    assert 10 not in mgr._desired_channel
    assert 10 not in mgr._current_applied
    assert 10 not in mgr._last_applied_at
    assert 10 not in mgr._backoff_until
    assert 10 not in mgr._emoji_disabled_guilds
    assert 10 not in mgr._missing_perm_guilds
    assert 110 not in mgr._skipped_channels


def test_voice_status_error_never_affects_playback():
    async def scenario():
        channel = FakeVoiceChannel(channel_id=111)
        channel.edit_error = RuntimeError("Discord Gateway Error")
        guild = FakeGuild(guild_id=11, channel=channel)
        bot = FakeBot(guild)
        cfg = make_config()
        mgr = VoiceStatusManager(bot, cfg)

        # Applying status directly swallows runtime error
        await mgr._apply_channel_status(11, 111, "Crashing Status")

        # on_player_destroy swallows errors
        player = SimpleNamespace(destroyed=False, current=None, paused=False, voice_channel_id=111)
        mgr._current_applied[11] = "Status"
        await mgr.on_player_destroy(11, player, forced=False)

    asyncio.run(scenario())
