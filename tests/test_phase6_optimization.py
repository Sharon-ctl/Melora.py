"""Phase 6 tests: Rate limiter, View lifecycle, Catch-all guard, Error mappings, Timing."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import discord
from discord import app_commands

from utils import messages
from utils.components_v2 import ACTIVE_VIEWS, ActionRow, BaseCardView, secondary_button
from utils.errors import ServerBusy, TookTooLong, format_interaction_error
from utils.ratelimit import RateLimited, RateLimiter
from utils.timing import (
    clear_timing_buffers,
    get_command_timing_stats,
    get_http_429_count,
    install_http_rate_limit_filter,
    record_ack_latency,
    record_handler_time,
    reset_http_429_count,
)


# ---------------------------------------------------------------------------
# Rate Limiter Tests
# ---------------------------------------------------------------------------


def test_rate_limiter_window_math_and_rounding():
    fake_time = 100.0

    def clock():
        return fake_time

    cfg = {
        "buckets": {
            "commands": {"rate": 3.0, "per": 10.0},
            "play": {"rate": 2.0, "per": 10.0},
            "components": {"rate": 2.0, "per": 4.0},
            "guild": {"rate": 10.0, "per": 10.0},
        },
        "max_keys": 100,
        "idle_ttl": 60.0,
    }
    limiter = RateLimiter(config=cfg, clock=clock)

    # 1. Acquire up to limit
    allowed, retry = limiter.acquire_command(user_id=1, guild_id=10, command_name="ping")
    assert allowed is True
    assert retry == 0.0

    allowed, retry = limiter.acquire_command(user_id=1, guild_id=10, command_name="ping")
    assert allowed is True

    allowed, retry = limiter.acquire_command(user_id=1, guild_id=10, command_name="ping")
    assert allowed is True

    # 4th request exceeds command limit (3 per 10s)
    allowed, retry = limiter.acquire_command(user_id=1, guild_id=10, command_name="ping")
    assert allowed is False
    assert 9.0 <= retry <= 10.0

    # Advance time by 5s -> still on cooldown (5s remaining)
    fake_time = 105.0
    allowed, retry = limiter.acquire_command(user_id=1, guild_id=10, command_name="ping")
    assert allowed is False
    assert 4.0 <= retry <= 5.0

    # Advance time by 6s (total 11s) -> window slid, request allowed
    fake_time = 111.0
    allowed, retry = limiter.acquire_command(user_id=1, guild_id=10, command_name="ping")
    assert allowed is True


def test_rate_limiter_owner_bypass():
    fake_time = 100.0
    cfg = {"max_keys": 50}
    limiter = RateLimiter(config=cfg, clock=lambda: fake_time)

    # Owner bypasses command limits indefinitely
    for _ in range(20):
        allowed, retry = limiter.acquire_command(user_id=999, guild_id=10, command_name="play", owner_id=999)
        assert allowed is True
        assert retry == 0.0

    # Owner bypasses component limits
    for _ in range(20):
        allowed, retry = limiter.acquire_component(user_id=999, owner_id=999)
        assert allowed is True
        assert retry == 0.0

    # Owner bypasses autocomplete limits
    for _ in range(20):
        allowed, retry = limiter.acquire_autocomplete(user_id=999, owner_id=999)
        assert allowed is True
        assert retry == 0.0


def test_rate_limiter_key_expiry_and_cap():
    fake_time = 100.0

    def clock():
        return fake_time

    cfg = {"buckets": {"commands": {"rate": 10.0, "per": 10.0}}, "max_keys": 5, "idle_ttl": 60.0}
    limiter = RateLimiter(config=cfg, clock=clock)

    # Fill up 5 keys
    for uid in range(1, 6):
        limiter.acquire_command(user_id=uid, guild_id=10, command_name="ping")
    assert len(limiter._windows) == 5

    # 6th key causes LRU eviction of oldest key
    limiter.acquire_command(user_id=6, guild_id=10, command_name="ping")
    assert len(limiter._windows) <= 5

    # Advance clock and prune idle keys
    fake_time = 500.0
    pruned = limiter.prune_idle(max_idle_seconds=60.0)
    assert pruned > 0
    assert len(limiter._windows) == 0


# ---------------------------------------------------------------------------
# View Lifecycle & ActiveViews Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_view_timeout_disables_items_and_edits_partial_message():
    view = BaseCardView(timeout=120.0, author_id=123, guild_id=10, kind="test")
    btn = secondary_button("Click")
    row = ActionRow(btn)
    view.add_item(row)

    view.channel_id = 456
    view.message_id = 789

    mock_channel = MagicMock()
    mock_partial = MagicMock()
    mock_partial.edit = AsyncMock()
    mock_channel.get_partial_message.return_value = mock_partial

    mock_client = MagicMock()
    mock_client.get_channel.return_value = mock_channel
    view._client = mock_client

    assert btn.disabled is False

    # Trigger timeout
    await view.on_timeout()

    assert btn.disabled is True
    mock_channel.get_partial_message.assert_called_once_with(789)
    mock_partial.edit.assert_awaited_once()
    assert view._client is None  # References released


@pytest.mark.anyio
async def test_active_views_second_view_stops_first():
    view1 = BaseCardView(timeout=120.0, author_id=100, guild_id=5, kind="queue")
    btn1 = secondary_button("V1")
    view1.add_item(ActionRow(btn1))

    view2 = BaseCardView(timeout=120.0, author_id=100, guild_id=5, kind="queue")
    btn2 = secondary_button("V2")
    view2.add_item(ActionRow(btn2))

    await ACTIVE_VIEWS.register(view1)
    key = (5, 100, "queue")
    assert ACTIVE_VIEWS._views.get(key) is view1

    # Registering second view stops first view and disables its buttons
    await ACTIVE_VIEWS.register(view2)
    assert view1._stopped_or_timed_out is True
    assert btn1.disabled is True
    assert ACTIVE_VIEWS._views.get(key) is view2

    await ACTIVE_VIEWS.close_all()
    assert len(ACTIVE_VIEWS._views) == 0


@pytest.mark.anyio
async def test_view_not_your_menu_and_rate_limited():
    limiter = RateLimiter()
    mock_client = SimpleNamespace(rate_limiter=limiter, cfg=SimpleNamespace(owner_id=1))

    view = BaseCardView(timeout=120.0, author_id=100, guild_id=5, kind="queue")

    # Stranger clicks -> not your menu
    interaction_stranger = SimpleNamespace(
        user=SimpleNamespace(id=999),
        client=mock_client,
        response=AsyncMock(),
    )
    interaction_stranger.response.is_done.return_value = False
    assert await view.interaction_check(interaction_stranger) is False
    interaction_stranger.response.send_message.assert_awaited_once()
    call_msg = interaction_stranger.response.send_message.call_args[0][0]
    assert call_msg == messages.not_your_menu()

    # Author clicks 4 times -> 4th is rate limited (components limit is 3 per 3s)
    interaction_author = SimpleNamespace(
        user=SimpleNamespace(id=100),
        client=mock_client,
        response=AsyncMock(),
    )
    interaction_author.response.is_done.return_value = False

    # 3 allowed
    for _ in range(3):
        assert await view.interaction_check(interaction_author) is True

    # 4th blocked
    assert await view.interaction_check(interaction_author) is False
    assert interaction_author.response.send_message.called
    rl_msg = interaction_author.response.send_message.call_args[0][0]
    assert "Rate limited" in rl_msg


# ---------------------------------------------------------------------------
# Catch-all Guard Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_guard_unanswered_component_gets_expired_message():
    from main import MusicBot

    mock_bot = MagicMock()
    mock_interaction = MagicMock()
    mock_interaction.response.is_done.return_value = False
    mock_interaction.response.send_message = AsyncMock()

    # Run guard with minimal delay for fast test
    with patch("asyncio.sleep", AsyncMock()):
        await MusicBot._guard_component_interaction(mock_bot, mock_interaction)

    mock_interaction.response.send_message.assert_awaited_once()
    call_args, call_kwargs = mock_interaction.response.send_message.call_args
    assert call_args[0] == messages.menu_expired()
    assert call_kwargs.get("ephemeral") is True


@pytest.mark.anyio
async def test_guard_answered_component_does_nothing():
    from main import MusicBot

    mock_bot = MagicMock()
    mock_interaction = MagicMock()
    mock_interaction.response.is_done.return_value = True
    mock_interaction.response.send_message = AsyncMock()

    with patch("asyncio.sleep", AsyncMock()):
        await MusicBot._guard_component_interaction(mock_bot, mock_interaction)

    mock_interaction.response.send_message.assert_not_called()


@pytest.mark.anyio
async def test_guard_stale_nowplaying_card_deleted():
    from main import MusicBot

    mock_bot = MagicMock()
    mock_player = MagicMock()
    mock_player._last_card_message_id = 999  # Current card is 999
    mock_bot.registry.get.return_value = mock_player

    stale_message = MagicMock()
    stale_message.id = 888  # Clicked card is 888 (stale)
    stale_message.delete = AsyncMock()

    mock_interaction = MagicMock()
    mock_interaction.guild_id = 1
    mock_interaction.message = stale_message
    mock_interaction.data = {"custom_id": "np:skip"}
    mock_interaction.response.is_done.return_value = False
    mock_interaction.response.send_message = AsyncMock()

    with patch("asyncio.sleep", AsyncMock()):
        await MusicBot._guard_component_interaction(mock_bot, mock_interaction)

    stale_message.delete.assert_awaited_once()


# ---------------------------------------------------------------------------
# Error Handler Mapping Tests
# ---------------------------------------------------------------------------


def test_format_interaction_error_mappings():
    interaction = MagicMock()
    interaction.command = SimpleNamespace(qualified_name="play")

    # 1. RateLimited
    err_rl = RateLimited(4.2)
    assert format_interaction_error(interaction, err_rl) == messages.rate_limited(5)

    # 2. CommandOnCooldown
    err_cd = app_commands.CommandOnCooldown(app_commands.Cooldown(1.0, 5.0), 3.1)
    assert format_interaction_error(interaction, err_cd) == messages.rate_limited(4)

    # 3. CommandSignatureMismatch & CommandNotFound
    err_sig = app_commands.CommandSignatureMismatch(MagicMock())
    assert format_interaction_error(interaction, err_sig) == messages.command_outdated()

    err_cnf = app_commands.CommandNotFound("foo", [])
    assert format_interaction_error(interaction, err_cnf) == messages.command_outdated()

    # 4. TimeoutError
    err_to = asyncio.TimeoutError()
    assert format_interaction_error(interaction, err_to) == messages.took_too_long()

    # 5. ServerBusy & TookTooLong
    assert format_interaction_error(interaction, ServerBusy()) == messages.server_busy()
    assert format_interaction_error(interaction, TookTooLong()) == messages.took_too_long()

    # 6. Discord NotFound (10062) & HTTPException (40060) swallowed
    nf_10062 = discord.NotFound(MagicMock(status=404), {"code": 10062, "message": "Unknown interaction"})
    assert format_interaction_error(interaction, nf_10062) is None

    http_40060 = discord.HTTPException(
        MagicMock(status=400), {"code": 40060, "message": "Interaction has already been acknowledged."}
    )
    assert format_interaction_error(interaction, http_40060) is None

    # 7. Discord 429
    http_429 = discord.HTTPException(MagicMock(status=429), "rate limited")
    http_429.status = 429
    http_429.retry_after = 2.4
    assert format_interaction_error(interaction, http_429) == messages.rate_limited(3)

    # 8. CheckFailure
    assert format_interaction_error(interaction, app_commands.CheckFailure()) == messages.check_failure()

    # 9. TransformerError
    trans_err = app_commands.TransformerError("v", discord.AppCommandOptionType.string, MagicMock())
    assert format_interaction_error(interaction, trans_err) == messages.invalid_input()

    # 10. Discord Forbidden (Bot missing permission)
    forbidden_err = discord.Forbidden(MagicMock(status=403), "Missing Access")
    assert format_interaction_error(interaction, forbidden_err) == messages.bot_forbidden()

    # 11. Discord Forbidden wrapped in CommandInvokeError
    cmd_invoke_err = app_commands.CommandInvokeError(MagicMock(), forbidden_err)
    assert format_interaction_error(interaction, cmd_invoke_err) == messages.bot_forbidden()

    # 12. app_commands.MissingPermissions (User missing permission)
    missing_user_perms = app_commands.MissingPermissions(["send_messages"])
    assert format_interaction_error(interaction, missing_user_perms) == messages.missing_permissions()


def test_view_on_error_forbidden():
    async def scenario():
        view = BaseCardView()
        interaction = AsyncMock()
        interaction.response.is_done.return_value = False
        interaction.command = None
        forbidden_err = discord.Forbidden(MagicMock(status=403), "Missing Access")
        await view.on_error(interaction, forbidden_err, MagicMock())
        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.call_args[0][0]
        assert msg == messages.bot_forbidden()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Timing & Rate Limit Filter Tests
# ---------------------------------------------------------------------------


def test_timing_percentiles_and_warning(caplog):
    clear_timing_buffers()

    # Record 10 samples
    for i in range(1, 11):
        record_ack_latency("play", i * 0.05)  # 50ms to 500ms
        record_handler_time("play", i * 0.1)  # 100ms to 1000ms

    stats = get_command_timing_stats()
    assert "play" in stats
    play_stats = stats["play"]
    assert play_stats["sample_count"] == 10
    assert play_stats["ack_p50_ms"] > 0
    assert play_stats["total_p50_ms"] > 0

    # Warning logged when exceeding 1.5s
    with caplog.at_level(logging.WARNING):
        record_handler_time("play", 1.8)
    assert any("execution took 1.800s (> 1.5s budget)" in record.message for record in caplog.records)

    clear_timing_buffers()


def test_discord_http_429_filter():
    reset_http_429_count()
    install_http_rate_limit_filter()

    http_logger = logging.getLogger("discord.http")
    http_logger.warning("We are being rate limited. Retrying in 2.50 seconds. (429)")

    assert get_http_429_count() >= 1
    reset_http_429_count()
    assert get_http_429_count() == 0
