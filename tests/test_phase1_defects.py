"""Regression tests for Phase 1 defect fixes.

Tests:
1. Prevention of 'Event loop error: Future exception was never retrieved'
   during failing loads, timeouts, cancellations, and shutdown.
2. Load failure preservation of underlying Lavalink error (message, severity, source)
   and plain-text logging without Discord markdown.
3. Timing warning thresholds (1.0s ack latency, 6.0s budget for /play and /search).
4. Concurrent track load + voice connect in /play and immediate deferral.
"""
from __future__ import annotations

import asyncio
import gc
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from lavalink.server import LoadResult

from core.contracts import PlayerServices
from core.lavalink_service import LavalinkService
from core.queue import QueueItem
from core.registry import PlayerRegistry
from tests.fakes import FakeBackend, FakeLoader, make_config, make_track
from utils.errors import LoadFailed
from utils.timing import (
    clear_timing_buffers,
    record_ack_latency,
    record_handler_time,
)


class MockLavalinkClient:
    def __init__(self) -> None:
        self.node_manager = SimpleNamespace(available_nodes=[SimpleNamespace(name="main", available=True)])
        self.get_tracks_coro = None

    async def get_tracks(self, identifier: str) -> LoadResult:
        if self.get_tracks_coro:
            return await self.get_tracks_coro(identifier)
        return LoadResult.from_dict({"loadType": "EMPTY", "data": []})


def test_no_unretrieved_exception_on_failing_load():
    """Verify that failing single-flight loads never leave un-retrieved future exceptions."""
    async def scenario():
        loop = asyncio.get_running_loop()
        recorded: list[dict[str, Any]] = []

        def handler(_loop, context: dict[str, Any]) -> None:
            recorded.append(context)

        loop.set_exception_handler(handler)

        cfg = make_config()
        client = MockLavalinkClient()

        async def _failing_get_tracks(identifier: str) -> LoadResult:
            await asyncio.sleep(0.01)
            return LoadResult.from_dict({
                "loadType": "ERROR",
                "data": {
                    "message": "Video is private",
                    "severity": "COMMON",
                    "cause": "Private video",
                    "causeStackTrace": "",
                },
            })

        client.get_tracks_coro = _failing_get_tracks
        service = LavalinkService(client, cfg)  # type: ignore[arg-type]

        with pytest.raises(LoadFailed) as exc_info:
            await service.load(123, "ytmsearch:private song")

        assert exc_info.value.cause == "Video is private"
        assert str(exc_info.value.severity).lower() == "common"

        gc.collect()
        await asyncio.sleep(0.05)
        gc.collect()

        unretrieved = [
            ctx for ctx in recorded
            if "never retrieved" in ctx.get("message", "").lower()
        ]
        assert len(unretrieved) == 0, f"Found unretrieved exceptions: {unretrieved}"

    asyncio.run(scenario())


def test_no_unretrieved_exception_on_timeout_and_cancel():
    """Verify timeouts and cancellations do not leave un-retrieved future exceptions."""
    async def scenario():
        loop = asyncio.get_running_loop()
        recorded: list[dict[str, Any]] = []

        def handler(_loop, context: dict[str, Any]) -> None:
            recorded.append(context)

        loop.set_exception_handler(handler)

        cfg = make_config()
        client = MockLavalinkClient()

        async def _slow_failing_tracks(identifier: str) -> LoadResult:
            await asyncio.sleep(0.1)
            raise RuntimeError("Late failure in audio backend")

        client.get_tracks_coro = _slow_failing_tracks
        service = LavalinkService(client, cfg)  # type: ignore[arg-type]

        with pytest.raises((TimeoutError, LoadFailed)):
            async with asyncio.timeout(0.02):
                await service.load(123, "ytmsearch:timeout song")

        await asyncio.sleep(0.15)
        gc.collect()

        unretrieved = [
            ctx for ctx in recorded
            if "never retrieved" in ctx.get("message", "").lower()
        ]
        assert len(unretrieved) == 0, f"Found unretrieved exceptions: {unretrieved}"

    asyncio.run(scenario())


def test_shutdown_during_failing_load():
    """Simulate shutdown where tasks are cancelled while loads are failing."""
    async def scenario():
        loop = asyncio.get_running_loop()
        recorded: list[dict[str, Any]] = []

        def handler(_loop, context: dict[str, Any]) -> None:
            recorded.append(context)

        loop.set_exception_handler(handler)

        cfg = make_config()
        client = MockLavalinkClient()

        started_event = asyncio.Event()

        async def _stalled_load(identifier: str) -> LoadResult:
            started_event.set()
            await asyncio.Event().wait()  # Never finishes unless cancelled
            return LoadResult.from_dict({"loadType": "EMPTY", "data": []})

        client.get_tracks_coro = _stalled_load
        service = LavalinkService(client, cfg)  # type: ignore[arg-type]

        task = asyncio.create_task(service.load(456, "ytmsearch:shutdown test"))
        await started_event.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        gc.collect()
        await asyncio.sleep(0.05)
        gc.collect()

        unretrieved = [
            ctx for ctx in recorded
            if "never retrieved" in ctx.get("message", "").lower()
        ]
        assert len(unretrieved) == 0, f"Found unretrieved exceptions: {unretrieved}"

    asyncio.run(scenario())


def test_load_failure_preserves_cause_and_plain_text_logging(caplog):
    """Verify LoadFailed and NoMatches preserve underlying errors and log in plain text without markdown."""
    async def scenario():
        cfg = make_config()
        client = MockLavalinkClient()

        async def _error_tracks(identifier: str) -> LoadResult:
            return LoadResult.from_dict({
                "loadType": "ERROR",
                "data": {
                    "message": "Sign in to confirm your age",
                    "severity": "COMMON",
                    "cause": "Age restricted",
                },
            })

        client.get_tracks_coro = _error_tracks
        service = LavalinkService(client, cfg)  # type: ignore[arg-type]

        with caplog.at_level(logging.INFO):
            with pytest.raises(LoadFailed) as exc_info:
                await service.load(123, "ytmsearch:restricted song")

        err = exc_info.value
        assert err.cause == "Sign in to confirm your age"
        assert str(err.severity).lower() == "common"
        assert err.source in ("ytmsearch", "ytsearch", "scsearch")

        # Confirm log output is plain text without markdown stars or backticks
        load_logs = [r.message for r in caplog.records if "Load attempt with source" in r.message]
        assert len(load_logs) > 0
        for log_msg in load_logs:
            assert "**" not in log_msg
            assert "`" not in log_msg
            assert "LoadFailed" in log_msg
            assert "cause=Sign in to confirm your age" in log_msg

    asyncio.run(scenario())


def test_timing_warnings_and_budgets(caplog):
    """Verify ack warning triggers at > 1.0s and handler budget is 6.0s for play/search."""
    clear_timing_buffers()

    # Ack latency under 1.0s -> no warning
    with caplog.at_level(logging.WARNING):
        record_ack_latency("play", 0.7)
    assert not any("ack latency" in r.message for r in caplog.records)

    # Ack latency > 1.0s -> warning
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        record_ack_latency("play", 1.2)
    assert any("Interaction ack latency for /play took 1.200s (> 1.0s budget)" in r.message for r in caplog.records)

    # Handler time for play at 2.5s (under 6.0s budget) -> no warning
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        record_handler_time("play", 2.5)
    assert not any("execution took" in r.message for r in caplog.records)

    # Handler time for play at 6.2s -> warning
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        record_handler_time("play", 6.2)
    assert any("Command /play execution took 6.200s (> 6.0s budget)" in r.message for r in caplog.records)

    # In-memory command at 1.8s (exceeds default 1.5s budget) -> warning
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        record_handler_time("pause", 1.8)
    assert any("Command /pause execution took 1.800s (> 1.5s budget)" in r.message for r in caplog.records)

    clear_timing_buffers()


def test_play_command_defers_first_and_gathers_concurrently():
    """Verify /play defers immediately and runs load and connect concurrently."""
    async def scenario():
        import discord
        from cogs.music import Music

        bot = MagicMock()
        bot.cfg = make_config()
        backend = FakeBackend()
        loader = FakeLoader()
        registry = PlayerRegistry(PlayerServices(bot.cfg, backend, loader))
        bot.registry = registry
        bot.loader = loader

        cog = Music(bot)

        interaction = MagicMock()
        interaction.guild_id = 999
        interaction.channel_id = 888
        interaction.user = MagicMock(spec=discord.Member)
        interaction.user.id = 111
        interaction.user.display_name = "User"
        interaction.user.display_avatar.url = "https://example.com/avatar.png"
        interaction.guild = MagicMock()
        interaction.guild.id = 999

        # Mock member and voice channel
        voice_state = MagicMock()
        voice_channel = MagicMock()
        voice_channel.id = 777
        voice_state.channel = voice_channel
        interaction.user.voice = voice_state

        # Track call order
        call_order: list[str] = []

        async def mock_defer(*args, **kwargs):
            call_order.append("defer")
            return True

        async def mock_load(*args, **kwargs):
            call_order.append("load_start")
            await asyncio.sleep(0.02)
            call_order.append("load_done")
            return [QueueItem.from_track(make_track(1), 111)], 0, None, 1

        real_get_or_create = registry.get_or_create

        async def mock_get_or_create(*args, **kwargs):
            call_order.append("connect_start")
            await asyncio.sleep(0.02)
            call_order.append("connect_done")
            p = await real_get_or_create(999, 777, 888)
            return p

        with (
            patch("cogs.music.safe_defer", side_effect=mock_defer),
            patch.object(cog, "_load_query_items", side_effect=mock_load),
            patch.object(registry, "get_or_create", side_effect=mock_get_or_create),
            patch("cogs.music.reply", new_callable=AsyncMock),
        ):
            await cog.play.callback(cog, interaction, "test song")

        # Verify defer happened before load and connect started
        assert call_order[0] == "defer"
        # Verify load and connect were both started before either finished (concurrent!)
        assert "load_start" in call_order[:3]
        assert "connect_start" in call_order[:3]

    asyncio.run(scenario())
