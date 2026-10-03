"""Numbers shared by the heartbeat log line and the /status command."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import psutil

from utils.text import format_uptime

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from main import MusicBot

_PROCESS = psutil.Process(os.getpid())


@dataclass(frozen=True)
class Stats:
    guilds: int
    players: int
    queued: int
    tasks: int
    supervised: int
    rss_mb: float
    latency_ms: int
    uptime_seconds: float
    nodes: tuple[tuple[str, bool], ...]


def rss_mb() -> float:
    try:
        return _PROCESS.memory_info().rss / (1024 * 1024)
    except Exception as exc:
        log.debug("Memory query failed: %s", exc)
        return 0.0


def collect_stats(bot: MusicBot) -> Stats:
    registry = getattr(bot, "registry", None)
    latency = bot.latency
    latency_ms = int(latency * 1000) if math.isfinite(latency) else -1
    lavalink_client = getattr(bot, "lavalink", None)
    nodes: tuple[tuple[str, bool], ...] = ()
    if lavalink_client is not None:
        nodes = tuple((str(node.name), bool(node.available)) for node in lavalink_client.node_manager.nodes)
    try:
        task_count = len(asyncio.all_tasks())
    except RuntimeError as exc:
        log.debug("all_tasks query failed: %s", exc)
        task_count = 0
    return Stats(
        guilds=len(bot.guilds),
        players=len(registry) if registry is not None else 0,
        queued=registry.total_queued() if registry is not None else 0,
        tasks=task_count,
        supervised=bot.supervisor.live_count(),
        rss_mb=rss_mb(),
        latency_ms=latency_ms,
        uptime_seconds=time.monotonic() - bot.started_at,
        nodes=nodes,
    )


def heartbeat_line(stats: Stats) -> str:
    nodes = ",".join(f"{name}={'up' if up else 'down'}" for name, up in stats.nodes) or "none"
    return (
        f"heartbeat guilds={stats.guilds} players={stats.players} queued={stats.queued} "
        f"tasks={stats.tasks} supervised={stats.supervised} rss_mb={stats.rss_mb:.1f} "
        f"latency_ms={stats.latency_ms} nodes={nodes}"
    )


def status_text(stats: Stats) -> str:
    nodes = ", ".join(f"{name} {'up' if up else 'down'}" for name, up in stats.nodes) or "none"
    latency = f"{stats.latency_ms} ms" if stats.latency_ms >= 0 else "unknown"
    return "\n".join(
        [
            f"Guilds: {stats.guilds}",
            f"Players: {stats.players}",
            f"Queued tracks: {stats.queued}",
            f"Tasks: {stats.tasks} total, {stats.supervised} supervised",
            f"Memory: {stats.rss_mb:.1f} MB",
            f"Gateway latency: {latency}",
            f"Uptime: {format_uptime(stats.uptime_seconds)}",
            f"Nodes: {nodes}",
        ]
    )
