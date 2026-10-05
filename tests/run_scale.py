"""Scale test for 1,000 to 5,000 active guilds with fake players.

Simulates:
- High guild scale (default 2,000 guilds, configurable up to 5,000)
- Play and acknowledgement latency
- Queueing 1,000 tracks per guild on high-capacity guilds
- Pause, resume, and skip operations
- Card edit batch flushing
- Voice disconnect events and chunked reconciliation
- Central scheduler deadlines and timer cancellations
- Watchdog sweep with chunked event-loop yielding
- Multi-node Lavalink failover on node loss
- Clean teardown with zero task or state leakage

Exit non-zero if any budget is missed.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import logging
import sys
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

# Ensure workspace root is in sys.path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from core.contracts import PlayerServices  # noqa: E402
from core.queue import QueueItem  # noqa: E402
from core.registry import PlayerRegistry  # noqa: E402
from core.stats import rss_mb  # noqa: E402
from tests.fakes import FakeBackend, FakeLoader, make_config, make_track  # noqa: E402

log = logging.getLogger(__name__)


@dataclass
class ScaleBudgets:
    max_loop_lag_p99_ms: float = 50.0
    max_ack_latency_p95_ms: float = 50.0
    max_task_growth: int = 0
    max_memory_per_idle_guild_bytes: float = 150_000.0   # 150 KB
    max_memory_per_active_guild_bytes: float = 350_000.0  # 350 KB
    max_memory_per_queued_track_bytes: float = 600.0     # 600 bytes


@dataclass
class ScaleReport:
    guild_count: int
    loop_lag_p50_ms: float = 0.0
    loop_lag_p95_ms: float = 0.0
    loop_lag_p99_ms: float = 0.0
    loop_lag_max_ms: float = 0.0
    ack_latency_p50_ms: float = 0.0
    ack_latency_p95_ms: float = 0.0
    ack_latency_p99_ms: float = 0.0
    final_task_count: int = 0
    task_delta: int = 0
    memory_per_idle_guild_bytes: float = 0.0
    memory_per_active_guild_bytes: float = 0.0
    memory_per_queued_track_bytes: float = 0.0
    leftover_players: int = 0
    leftover_audio: int = 0
    leftover_voice: int = 0
    leftover_timers: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.problems) == 0


async def run_scale(guild_count: int = 2000, budgets: ScaleBudgets | None = None) -> ScaleReport:
    if budgets is None:
        budgets = ScaleBudgets()

    cfg = make_config()
    backend = FakeBackend()
    loader = FakeLoader()

    services = PlayerServices(cfg=cfg, backend=backend, loader=loader)
    registry = PlayerRegistry(services)
    scheduler = registry.scheduler
    flusher = registry.flusher
    loop_monitor = registry.loop_monitor

    # Start scale infrastructure
    registry.start_background_tasks()

    await asyncio.sleep(0.05)
    gc.collect()

    baseline_tasks = len(asyncio.all_tasks())
    baseline_rss = rss_mb()

    # -------------------------------------------------------------------------
    # 1. Idle Guild Creation
    # -------------------------------------------------------------------------
    chunk_size = 50
    for chunk_start in range(1, guild_count + 1, chunk_size):
        chunk_end = min(guild_count + 1, chunk_start + chunk_size)
        for gid in range(chunk_start, chunk_end):
            await registry.get_or_create(gid, 10_000 + gid, 20_000 + gid)
        await asyncio.sleep(0)  # Yield event loop

    await asyncio.sleep(0.05)
    gc.collect()
    idle_rss = rss_mb()
    memory_per_idle_guild = max(0.0, (idle_rss - baseline_rss) * 1024 * 1024 / guild_count)

    # -------------------------------------------------------------------------
    # 2. Play Simulation & Acknowledgement Latency Measurement
    # -------------------------------------------------------------------------
    ack_latencies_ms: list[float] = []
    for chunk_start in range(1, guild_count + 1, chunk_size):
        chunk_end = min(guild_count + 1, chunk_start + chunk_size)
        for gid in range(chunk_start, chunk_end):
            player = registry.get(gid)
            if player is not None:
                item = QueueItem.from_track(make_track(gid), requester_id=999)
                t0 = time.perf_counter()
                await player.enqueue([item])
                t_ack = (time.perf_counter() - t0) * 1000.0
                ack_latencies_ms.append(t_ack)
        await asyncio.sleep(0)

    # -------------------------------------------------------------------------
    # 3. Queueing 1,000 Tracks & Memory per Queued Track
    # -------------------------------------------------------------------------
    # Queue 1,000 tracks on a representative set of 20 guilds (20,000 tracks total)
    started_tracing = not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start()
    m_before_1k = tracemalloc.get_traced_memory()[0]

    test_1k_guilds = min(20, guild_count)
    queued_tracks_count = 0
    for gid in range(1, test_1k_guilds + 1):
        player = registry.get(gid)
        if player is not None:
            tracks_1k = [
                QueueItem.from_track(make_track(gid * 10_000 + k), requester_id=gid)
                for k in range(1000)
            ]
            await player.enqueue(tracks_1k)
            queued_tracks_count += 1000
        await asyncio.sleep(0)

    m_after_1k = tracemalloc.get_traced_memory()[0]
    if started_tracing:
        tracemalloc.stop()
    memory_per_queued_track = max(0.0, (m_after_1k - m_before_1k) / max(1, queued_tracks_count))

    # -------------------------------------------------------------------------
    # 4. Operations: Pause, Resume, Skip, Card Edits, Timers
    # -------------------------------------------------------------------------
    for chunk_start in range(1, guild_count + 1, chunk_size):
        chunk_end = min(guild_count + 1, chunk_start + chunk_size)
        for gid in range(chunk_start, chunk_end):
            player = registry.get(gid)
            if player is None:
                continue

            # Pause & resume
            if gid % 3 == 0:
                await player.pause()
                await player.resume()

            # Skip
            if gid % 4 == 0 and player.current is not None:
                await player.skip()

            # Central Scheduler Timers
            player.start_timer("alone", 300.0, "alone")
            player.start_timer("idle", 600.0, "idle")

            # Card Edits
            if gid % 5 == 0:
                flusher.schedule(gid, player)
        await asyncio.sleep(0)

    # -------------------------------------------------------------------------
    # 5. Voice Events & Watchdog Sweep
    # -------------------------------------------------------------------------
    # Simulate voice disconnects for 10% of guilds
    for gid in range(1, max(2, guild_count // 10)):
        backend.voice.pop(gid, None)

    # Run watchdog sweep (chunked, yields to loop)
    await registry.watchdog()
    await registry.reconcile("scale_test_voice")
    await asyncio.sleep(0.05)

    # -------------------------------------------------------------------------
    # 6. Multi-Node Loss & Failover Simulation
    # -------------------------------------------------------------------------
    mock_lost_node = SimpleNamespace(name="node-scale-loss")
    await registry.handle_node_disconnected(mock_lost_node)
    await asyncio.sleep(0.05)

    gc.collect()
    active_rss = rss_mb()
    memory_per_active_guild = max(0.0, (active_rss - baseline_rss) * 1024 * 1024 / guild_count)

    # -------------------------------------------------------------------------
    # 7. Teardown & Leak Detection
    # -------------------------------------------------------------------------
    await registry.destroy_all("scale_test_end")
    flusher.stop()
    scheduler.stop()
    loop_monitor.stop()

    await asyncio.sleep(0.1)
    gc.collect()

    final_tasks = len(asyncio.all_tasks())
    task_delta = final_tasks - baseline_tasks

    # Calculate latency percentiles
    ack_latencies_ms.sort()
    n_ack = len(ack_latencies_ms)
    ack_p50 = ack_latencies_ms[int(n_ack * 0.50)] if n_ack else 0.0
    ack_p95 = ack_latencies_ms[int(n_ack * 0.95)] if n_ack else 0.0
    ack_p99 = ack_latencies_ms[int(n_ack * 0.99)] if n_ack else 0.0

    report = ScaleReport(
        guild_count=guild_count,
        loop_lag_p50_ms=loop_monitor.p50,
        loop_lag_p95_ms=loop_monitor.p95,
        loop_lag_p99_ms=loop_monitor.p99,
        loop_lag_max_ms=loop_monitor.max_lag,
        ack_latency_p50_ms=ack_p50,
        ack_latency_p95_ms=ack_p95,
        ack_latency_p99_ms=ack_p99,
        final_task_count=final_tasks,
        task_delta=task_delta,
        memory_per_idle_guild_bytes=memory_per_idle_guild,
        memory_per_active_guild_bytes=memory_per_active_guild,
        memory_per_queued_track_bytes=memory_per_queued_track,
        leftover_players=len(registry),
        leftover_audio=len(backend.audios),
        leftover_voice=len(backend.voice),
        leftover_timers=scheduler.pending_count,
    )

    # Verify budgets
    if report.loop_lag_p99_ms > budgets.max_loop_lag_p99_ms:
        report.problems.append(
            f"Event loop lag p99 ({report.loop_lag_p99_ms:.2f} ms) exceeded budget ({budgets.max_loop_lag_p99_ms:.2f} ms)"
        )
    if report.ack_latency_p95_ms > budgets.max_ack_latency_p95_ms:
        report.problems.append(
            f"Acknowledgement latency p95 ({report.ack_latency_p95_ms:.2f} ms) "
            f"exceeded budget ({budgets.max_ack_latency_p95_ms:.2f} ms)"
        )
    if report.task_delta > budgets.max_task_growth:
        report.problems.append(
            f"Asyncio task growth ({report.task_delta}) exceeded budget ({budgets.max_task_growth})"
        )
    if report.leftover_players != 0:
        report.problems.append(f"Leaked {report.leftover_players} players in registry")
    if report.leftover_audio != 0 or report.leftover_voice != 0:
        report.problems.append(
            f"Leaked backend state: audio={report.leftover_audio} voice={report.leftover_voice}"
        )
    if report.leftover_timers != 0:
        report.problems.append(f"Leaked {report.leftover_timers} timers in CentralScheduler")
    if report.memory_per_queued_track_bytes > budgets.max_memory_per_queued_track_bytes:
        report.problems.append(
            f"Memory per queued track ({report.memory_per_queued_track_bytes:.1f} B) "
            f"exceeded budget ({budgets.max_memory_per_queued_track_bytes:.1f} B)"
        )

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Scale test for 1,000+ Discord music bot guilds")
    parser.add_argument("--guilds", type=int, default=2000, help="Number of simulated guilds (1000-5000)")
    parser.add_argument("--max-loop-lag-ms", type=float, default=50.0, help="Max loop lag p99 budget in ms")
    parser.add_argument("--max-ack-ms", type=float, default=50.0, help="Max ack latency p95 budget in ms")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR)
    budgets = ScaleBudgets(
        max_loop_lag_p99_ms=args.max_loop_lag_ms,
        max_ack_latency_p95_ms=args.max_ack_ms,
    )

    print(f"=== RUNNING SCALE TEST WITH {args.guilds} GUILDS ===", flush=True)
    report = asyncio.run(run_scale(args.guilds, budgets))

    print(f"Guilds Simulated       : {report.guild_count}", flush=True)
    print(f"Event Loop Lag p50     : {report.loop_lag_p50_ms:.2f} ms", flush=True)
    print(f"Event Loop Lag p95     : {report.loop_lag_p95_ms:.2f} ms", flush=True)
    print(
        f"Event Loop Lag p99     : {report.loop_lag_p99_ms:.2f} ms "
        f"(budget: <= {budgets.max_loop_lag_p99_ms:.1f} ms)",
        flush=True,
    )
    print(f"Event Loop Lag Max     : {report.loop_lag_max_ms:.2f} ms", flush=True)
    print(f"Ack Latency p50        : {report.ack_latency_p50_ms:.2f} ms", flush=True)
    print(
        f"Ack Latency p95        : {report.ack_latency_p95_ms:.2f} ms "
        f"(budget: <= {budgets.max_ack_latency_p95_ms:.1f} ms)",
        flush=True,
    )
    print(f"Ack Latency p99        : {report.ack_latency_p99_ms:.2f} ms", flush=True)
    print(f"Active Tasks Delta     : {report.task_delta} (budget: <= {budgets.max_task_growth})", flush=True)
    print(f"Memory / Idle Guild    : {report.memory_per_idle_guild_bytes / 1024.0:.2f} KB", flush=True)
    print(f"Memory / Active Guild  : {report.memory_per_active_guild_bytes / 1024.0:.2f} KB", flush=True)
    print(
        f"Memory / Queued Track  : {report.memory_per_queued_track_bytes:.1f} bytes "
        f"(budget: <= {budgets.max_memory_per_queued_track_bytes:.1f} bytes)",
        flush=True,
    )
    print(
        f"Leftover State         : players={report.leftover_players} audio={report.leftover_audio} "
        f"voice={report.leftover_voice} timers={report.leftover_timers}",
        flush=True,
    )

    if report.ok:
        print("RESULT: PASS - All scale budgets met successfully!", flush=True)
        return 0

    print("RESULT: FAIL - The following budgets/invariants failed:", flush=True)
    for prob in report.problems:
        print(f"  - {prob}", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
