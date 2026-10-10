"""Interaction timing, latency percentiles, and Discord HTTP 429 tracking."""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any

log = logging.getLogger(__name__)

# Bounded ring buffers: command_name -> deque[float] (maxlen=100)
_ACK_LATENCY_BUFFERS: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=100))
_HANDLER_TIME_BUFFERS: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=100))

# Counter for Discord HTTP 429 rate limit warnings
_HTTP_429_COUNT: int = 0
_MAX_429_COUNT: int = 1_000_000


class DiscordRateLimitFilter(logging.Filter):
    """Logging filter on discord.http that counts rate-limit events without modifying records."""

    def filter(self, record: logging.LogRecord) -> bool:
        global _HTTP_429_COUNT
        msg = record.getMessage().lower()
        if "rate limit" in msg or "429" in msg or "retrying in" in msg:
            if _HTTP_429_COUNT < _MAX_429_COUNT:
                _HTTP_429_COUNT += 1
        return True


def install_http_rate_limit_filter() -> None:
    """Attach the 429 counter filter to the discord.http logger."""
    discord_http_logger = logging.getLogger("discord.http")
    for f in discord_http_logger.filters:
        if isinstance(f, DiscordRateLimitFilter):
            return
    discord_http_logger.addFilter(DiscordRateLimitFilter())


def get_http_429_count() -> int:
    return _HTTP_429_COUNT


def reset_http_429_count() -> None:
    global _HTTP_429_COUNT
    _HTTP_429_COUNT = 0


IO_BOUND_COMMAND_BUDGETS: dict[str, float] = {
    "play": 6.0,
    "search": 6.0,
    "playnext": 6.0,
    "insert": 6.0,
    "playinstant": 6.0,
    "similar": 6.0,
}
DEFAULT_HANDLER_BUDGET: float = 1.5
ACK_LATENCY_BUDGET: float = 1.0


def record_ack_latency(command: str, latency: float) -> None:
    """Record interaction acknowledgement latency in seconds, logging a warning if > 1.0s."""
    if not isinstance(latency, (int, float)):
        return
    val = max(0.0, float(latency))
    _ACK_LATENCY_BUFFERS[command].append(val)
    if val > ACK_LATENCY_BUDGET:
        log.warning("Interaction ack latency for /%s took %.3fs (> %.1fs budget)", command, val, ACK_LATENCY_BUDGET)


def record_handler_time(command: str, duration: float) -> None:
    """Record total command execution time in seconds, logging a warning if exceeding budget."""
    if not isinstance(duration, (int, float)):
        return
    val = max(0.0, float(duration))
    _HANDLER_TIME_BUFFERS[command].append(val)
    budget = IO_BOUND_COMMAND_BUDGETS.get(command, DEFAULT_HANDLER_BUDGET)
    if val > budget:
        log.warning("Command /%s execution took %.3fs (> %.1fs budget)", command, val, budget)


def calculate_percentiles(values: list[float]) -> tuple[float, float]:
    """Calculate p50 and p95 from a list of samples in seconds. Returns (p50_ms, p95_ms)."""
    if not values:
        return 0.0, 0.0
    s = sorted(values)
    n = len(s)
    p50_idx = int(n * 0.50)
    p95_idx = min(int(n * 0.95), n - 1)
    p50_ms = s[p50_idx] * 1000.0
    p95_ms = s[p95_idx] * 1000.0
    return p50_ms, p95_ms


def get_command_timing_stats() -> dict[str, dict[str, Any]]:
    """Return dict of command -> {ack_p50_ms, ack_p95_ms, total_p50_ms, total_p95_ms, sample_count}."""
    stats: dict[str, dict[str, Any]] = {}
    all_cmds = set(_ACK_LATENCY_BUFFERS.keys()) | set(_HANDLER_TIME_BUFFERS.keys())
    for cmd in sorted(all_cmds):
        ack_samples = list(_ACK_LATENCY_BUFFERS.get(cmd, []))
        dur_samples = list(_HANDLER_TIME_BUFFERS.get(cmd, []))
        ack_p50, ack_p95 = calculate_percentiles(ack_samples)
        dur_p50, dur_p95 = calculate_percentiles(dur_samples)
        stats[cmd] = {
            "ack_p50_ms": round(ack_p50, 1),
            "ack_p95_ms": round(ack_p95, 1),
            "total_p50_ms": round(dur_p50, 1),
            "total_p95_ms": round(dur_p95, 1),
            "sample_count": len(dur_samples),
        }
    return stats


def clear_timing_buffers() -> None:
    _ACK_LATENCY_BUFFERS.clear()
    _HANDLER_TIME_BUFFERS.clear()
