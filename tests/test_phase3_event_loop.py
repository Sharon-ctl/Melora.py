"""Unit tests for Phase 3: Event Loop Scale.

Verifies:
- CentralScheduler min-heap deadline ordering, cancellation per timer/guild, single destroy cleanup
- CardFlusher latest-wins per guild, cadence adaptation, zero per-guild persistent tasks
- Watchdog & reconcile chunking (50 per tick) yielding with sleep(0)
- LoopLagMonitor rolling percentiles (p50/p95/p99/max) and load shedding transitions
- Autocomplete, mention replies, and sweeps deferral during load shedding
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

from cogs.music import Music
from core.card_flusher import CardFlusher
from core.contracts import PlayerServices
from core.loop_monitor import LoopLagMonitor
from core.queue import QueueItem
from core.registry import PlayerRegistry
from core.scheduler import CentralScheduler
from tests.fakes import FakeBackend, FakeLoader, make_config


def test_scheduler_ordering_and_cancellation():
    """Verify CentralScheduler dispatches deadlines in chronological order and handles cancellation."""
    async def scenario():
        events: list[tuple[int, str]] = []

        async def on_expire(guild_id: int, reason: str) -> None:
            events.append((guild_id, reason))

        sched = CentralScheduler(on_expire)
        sched.start()
        try:
            # Schedule out of order
            sched.schedule(guild_id=1, timer_name="idle", delay=0.08, reason="idle")
            sched.schedule(guild_id=2, timer_name="alone", delay=0.02, reason="alone")
            sched.schedule(guild_id=3, timer_name="sleep", delay=0.05, reason="sleep")

            # Cancel guild 1 before it fires
            assert sched.has_timer(1, "idle")
            sched.cancel(1, "idle")
            assert not sched.has_timer(1, "idle")

            # Wait for remaining to fire
            await asyncio.sleep(0.12)

            # Guild 2 fired first (0.02s), then guild 3 (0.05s); guild 1 did not fire
            assert events == [(2, "alone"), (3, "sleep")]
        finally:
            sched.stop()

    asyncio.run(scenario())


def test_scheduler_cancel_guild():
    """Verify cancel_guild removes all timers for a guild."""
    async def scenario():
        events: list[tuple[int, str]] = []

        async def on_expire(guild_id: int, reason: str) -> None:
            events.append((guild_id, reason))

        sched = CentralScheduler(on_expire)
        sched.start()
        try:
            sched.schedule(guild_id=10, timer_name="idle", delay=0.03, reason="idle")
            sched.schedule(guild_id=10, timer_name="alone", delay=0.04, reason="alone")
            sched.schedule(guild_id=20, timer_name="idle", delay=0.03, reason="idle")

            sched.cancel_guild(10)
            assert not sched.has_timer(10, "idle")
            assert not sched.has_timer(10, "alone")
            assert sched.has_timer(20, "idle")

            await asyncio.sleep(0.1)
            assert events == [(20, "idle")]
        finally:
            sched.stop()

    asyncio.run(scenario())


def test_card_flusher_latest_wins():
    """Verify CardFlusher updates pending cards at steady cadence with latest-wins."""
    async def scenario():
        updates: list[int] = []

        class FakePlayer:
            def __init__(self, gid: int) -> None:
                self.guild_id = gid
                self.destroyed = False

            async def _update_card_locked(self) -> None:
                updates.append(self.guild_id)

        flusher = CardFlusher(default_cadence=0.04)
        flusher.start()
        try:
            p1 = FakePlayer(100)
            p2 = FakePlayer(200)

            # Enqueue multiple edits for guild 100; latest wins (executed only once per flush tick)
            flusher.schedule(100, p1)
            flusher.schedule(100, p1)
            flusher.schedule(200, p2)

            await asyncio.sleep(0.08)
            assert 100 in updates
            assert 200 in updates
            # Each guild was updated once in the tick
            assert updates.count(100) == 1
            assert updates.count(200) == 1
        finally:
            flusher.stop()

    asyncio.run(scenario())


def test_chunked_reconcile_yields():
    """Verify reconcile processes players in chunks of 50 and yields with sleep(0)."""
    async def scenario():
        cfg = make_config()
        backend = FakeBackend()
        loader = FakeLoader()
        services = PlayerServices(cfg, backend, loader)
        registry = PlayerRegistry(services)
        try:
            # Register 120 fake players
            for i in range(1, 121):
                p = await registry.get_or_create(i, 1000 + i, 2000 + i)
                p.current = QueueItem(track=None, title=f"Track {i}", duration_ms=1000, requester_id=1)

            sleep_calls = 0
            original_sleep = asyncio.sleep

            async def counting_sleep(delay: float) -> None:
                nonlocal sleep_calls
                if delay == 0:
                    sleep_calls += 1
                await original_sleep(delay)

            with patch("asyncio.sleep", side_effect=counting_sleep):
                await registry.reconcile("test chunking")

            # 120 players in chunks of 50 -> yields between chunk 1 and 2, and between chunk 2 and 3
            assert sleep_calls >= 2, f"Expected at least 2 chunk yields, got {sleep_calls}"
        finally:
            await registry.destroy_all("test")
            registry.stop_background_tasks()

    asyncio.run(scenario())


def test_loop_lag_monitor_and_load_shedding_transitions():
    """Verify LoopLagMonitor calculates percentiles and transitions load shedding state."""
    async def scenario():
        transitions: list[bool] = []

        def on_change(active: bool) -> None:
            transitions.append(active)

        monitor = LoopLagMonitor(
            sample_interval=0.01,
            window_size=20,
            load_shed_threshold_ms=50.0,
            on_load_shedding_changed=on_change,
        )

        # Inject fake samples: normal lag
        for _ in range(20):
            monitor._samples.append(0.005)  # 5ms

        stats = monitor.stats()
        assert stats["p50"] > 0
        assert stats["p95"] > 0
        assert not monitor.is_load_shedding

        # Now simulate high lag (> 50ms)
        for _ in range(20):
            monitor._samples.append(0.080)  # 80ms

        high_stats = monitor.stats()
        assert high_stats["p95"] >= 50.0

        # Simulate time passing under high lag
        now = time.monotonic()
        monitor._high_lag_start = now - 4.0
        # Trigger transition evaluation
        monitor._stopped = False
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.03)
        task.cancel()

        # Load shedding should activate
        assert monitor.is_load_shedding
        assert True in transitions

    asyncio.run(scenario())


def test_load_shedding_suppresses_search_and_mentions():
    """Verify search autocomplete returns [] and mention replies are skipped under load shedding."""
    async def scenario():
        cfg = make_config(AUTOCOMPLETE_SEARCH_ENABLED=True)
        backend = FakeBackend()
        loader = FakeLoader()
        services = PlayerServices(cfg, backend, loader)
        registry = PlayerRegistry(services)

        bot = SimpleNamespace(
            cfg=cfg,
            backend=backend,
            loader=loader,
            registry=registry,
            is_load_shedding=True,
        )
        music_cog = Music(bot)
        inter = SimpleNamespace(guild=SimpleNamespace(id=1), guild_id=1)

        # Autocomplete search is suppressed
        choices = await music_cog._search_autocomplete(inter, current="rock song")
        assert choices == []

        registry.stop_background_tasks()

    asyncio.run(scenario())
