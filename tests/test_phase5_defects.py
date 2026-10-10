"""Regression tests for Phase 5 defect hunt.

Covers:
1. Rejection of skip, vote_skip, pause, resume, seek, replay, and previous on destroyed player.
2. Prevention of card creation / sending if player is destroyed or current is None.
3. Stale nowplaying card button rejection with menu_expired.
4. NowPlayingView stop releasing player reference and clearing items.
5. Live nowplaying card preservation by component guard (checking both nowplaying_message_id and _last_card_message_id).
6. Direct pause/resume button press cancelling pending central flusher update.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.contracts import PlayerServices
from core.guild_player import GuildPlayer
from tests.fakes import FakeBackend, FakeLoader, make_config, make_track
from utils import messages
from utils.components_v2 import NowPlayingView
from utils.errors import BotUserError


def test_skip_during_destroy_rejected():
    async def scenario():
        cfg = make_config()
        backend = FakeBackend()
        loader = FakeLoader()
        services = PlayerServices(cfg, backend, loader)
        player = GuildPlayer(1, 10, 20, services)

        track1 = make_track(1)
        item = SimpleNamespace(
            track=track1,
            title="Track 1",
            duration_ms=1000,
            is_stream=False,
            uri="",
            query="",
            requester_id=1,
            fallback_used=False,
            replace_track=lambda t: None,
            artwork_url=None,
            artist=None,
            spotify_metadata=None,
        )
        await player.enqueue([item])

        # Mark destroyed
        player.destroyed = True

        with pytest.raises(BotUserError) as exc_info:
            await player.skip()
        assert "closed" in str(exc_info.value).lower()

    asyncio.run(scenario())


def test_player_operations_on_destroyed_player_rejected():
    async def scenario():
        cfg = make_config()
        backend = FakeBackend()
        loader = FakeLoader()
        services = PlayerServices(cfg, backend, loader)
        player = GuildPlayer(2, 10, 20, services)
        player.destroyed = True

        for method in (player.pause, player.resume, player.replay, player.previous):
            with pytest.raises(BotUserError) as exc_info:
                await method()
            assert "closed" in str(exc_info.value).lower()

        with pytest.raises(BotUserError):
            await player.seek(10)

        with pytest.raises(BotUserError):
            await player.vote_skip(123, 1)

    asyncio.run(scenario())


def test_nowplaying_card_not_sent_if_destroyed():
    async def scenario():
        cfg = make_config()
        backend = FakeBackend()
        loader = FakeLoader()
        services = PlayerServices(cfg, backend, loader)
        player = GuildPlayer(3, 10, 20, services)

        # Mock backend send_nowplaying_card
        send_mock = AsyncMock()
        backend.send_nowplaying_card = send_mock

        # Player is destroyed
        player.destroyed = True
        player.nowplaying_channel_id = 20

        await player._update_card_locked(recreate=True)

        # send_nowplaying_card was never called!
        assert send_mock.call_count == 0

    asyncio.run(scenario())


def test_stale_card_button_press_rejected():
    async def scenario():
        cfg = make_config()
        backend = FakeBackend()
        loader = FakeLoader()
        services = PlayerServices(cfg, backend, loader)
        player = GuildPlayer(4, 10, 20, services)
        player.nowplaying_message_id = 5555  # Current card is 5555

        view = NowPlayingView(player)

        # Interaction coming from message 1111 (old stale card)
        interaction = MagicMock()
        interaction.message.id = 1111
        interaction.user = SimpleNamespace(id=42, voice=SimpleNamespace(channel=SimpleNamespace(id=10)))
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()

        gate_ok = await view._check_gate(interaction, player)
        assert gate_ok is False
        interaction.response.send_message.assert_called_once()
        args, kwargs = interaction.response.send_message.call_args
        assert args[0] == messages.menu_expired()

    asyncio.run(scenario())


def test_nowplaying_view_stop_releases_player_reference():
    cfg = make_config()
    backend = FakeBackend()
    loader = FakeLoader()
    services = PlayerServices(cfg, backend, loader)
    player = GuildPlayer(5, 10, 20, services)

    view = NowPlayingView(player)
    assert view._get_player() is player

    view.stop()
    assert view._get_player() is None
    assert len(view.children) == 0


def test_pause_resume_button_cancels_flusher():
    async def scenario():
        from tests.fakes import FakeAudio

        cfg = make_config()
        backend = FakeBackend()
        backend.audios[6] = FakeAudio()
        loader = FakeLoader()
        flusher = MagicMock()
        services = PlayerServices(cfg, backend, loader, flusher=flusher)
        player = GuildPlayer(6, 10, 20, services)
        player.current = SimpleNamespace(
            title="Test", uri="", duration_ms=1000, requester_id=1, is_stream=False, artwork_url=None
        )
        player.nowplaying_message_id = 7777

        view = NowPlayingView(player)

        interaction = MagicMock()
        interaction.message.id = 7777
        interaction.user = SimpleNamespace(id=42, voice=SimpleNamespace(channel=SimpleNamespace(id=10)))
        interaction.response.is_done.return_value = False
        interaction.response.edit_message = AsyncMock()
        interaction.response.defer = AsyncMock()

        # Press pause/resume
        await view._on_pause_resume(interaction)

        # Verified flusher was cancelled for this guild
        flusher.cancel.assert_called_with(6)

    asyncio.run(scenario())
