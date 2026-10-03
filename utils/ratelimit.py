"""Centralized rate limiting for slash commands, interactive components, and views.

Features:
- Sliding-window rate limiter keyed by (bucket, entity_id).
- Configurable bucket definitions loaded from data/rate_limits.json.
- Injectable clock for deterministic testing.
- Bounded memory with LRU eviction and background idle key pruning.
- Complete owner bypass.
- Single ephemeral user-facing response with Pattern B message:
  **Rate limited** • `Try again in Ns`
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Any

import discord
from discord import app_commands

from core.data_loader import load_rate_limits
from utils import messages

log = logging.getLogger(__name__)

# Command category mapping
PLAY_COMMANDS = frozenset({"play", "insert", "playnext", "playinstant", "search"})
QUEUE_COMMANDS = frozenset({"remove", "clear", "shuffle", "move", "swap", "dedupe"})
LIBRARY_COMMANDS = frozenset(
    {
        "savequeue",
        "favorites add",
        "favorites remove",
        "favorites clear",
        "playlist create",
        "playlist delete",
        "playlist rename",
        "playlist add",
        "playlist remove",
    }
)
VIEW_COMMANDS = frozenset(
    {
        "help",
        "status",
        "queue",
        "history",
        "errors",
        "privacy",
        "settings view",
        "favorites list",
        "playlist list",
        "playlist view",
        "eq list",
        "filter list",
        "nowplaying",
    }
)


def get_command_category(command_name: str) -> str | None:
    """Return the bucket category for a qualified command name, or None."""
    cmd = command_name.lower().strip()
    if cmd in PLAY_COMMANDS:
        return "play"
    if cmd in QUEUE_COMMANDS:
        return "queue"
    if cmd in LIBRARY_COMMANDS:
        return "library"
    if cmd in VIEW_COMMANDS:
        return "views"
    return None


class RateLimited(app_commands.AppCommandError):
    """Raised when an interaction exceeds configured rate limits."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        self.retry_seconds = max(1, math.ceil(retry_after))
        super().__init__(messages.rate_limited(self.retry_seconds))


class RateLimiter:
    """Sliding-window central rate limiter with bounded memory and LRU eviction."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if config is None:
            try:
                config = load_rate_limits()
            except Exception as exc:
                log.warning("Failed loading rate_limits.json: %s; using defaults", exc)
                config = {
                    "buckets": {
                        "commands": {"rate": 5.0, "per": 10.0},
                        "play": {"rate": 4.0, "per": 10.0},
                        "queue": {"rate": 8.0, "per": 10.0},
                        "library": {"rate": 6.0, "per": 10.0},
                        "views": {"rate": 4.0, "per": 10.0},
                        "components": {"rate": 3.0, "per": 3.0},
                        "guild": {"rate": 30.0, "per": 10.0},
                    },
                    "max_keys": 10000,
                    "idle_ttl": 60.0,
                }

        self.buckets: dict[str, dict[str, float]] = config.get("buckets", {})
        self.max_keys: int = int(config.get("max_keys", 10000))
        self.idle_ttl: float = float(config.get("idle_ttl", 60.0))
        self.clock = clock

        # Key: (bucket_name, id) -> deque of timestamps
        self._entries: OrderedDict[tuple[str, int | str], deque[float]] = OrderedDict()
        self._last_seen: dict[tuple[str, int | str], float] = {}

    def get_bucket_cfg(self, bucket: str) -> tuple[float, float]:
        """Return (rate, per) for a bucket, falling back to safe defaults."""
        cfg = self.buckets.get(bucket)
        if cfg:
            return float(cfg["rate"]), float(cfg["per"])
        if bucket == "components":
            return 3.0, 3.0
        if bucket == "guild":
            return 30.0, 10.0
        return 5.0, 10.0

    def check_and_acquire(
        self,
        pairs: list[tuple[str, int | str]],
    ) -> tuple[bool, float]:
        """Check all candidate (bucket, id) pairs atomically.

        If ANY bucket is over the limit, returns (False, max_retry_after) without
        consuming any quota. If ALL are within limits, records the timestamp for
        each bucket and returns (True, 0.0).
        """
        now = self.clock()
        max_retry: float = 0.0
        exhausted = False

        # Phase 1: verify all pairs without modifying state
        for bucket, entity_id in pairs:
            rate, per = self.get_bucket_cfg(bucket)
            key = (bucket, entity_id)
            timestamps = self._entries.get(key)
            if not timestamps:
                continue

            # Window cutoff
            cutoff = now - per
            valid_count = sum(1 for ts in timestamps if ts > cutoff)
            if valid_count >= rate:
                # Oldest timestamp in window dictates retry time
                oldest_in_window = next(ts for ts in timestamps if ts > cutoff)
                retry = (oldest_in_window + per) - now
                if retry > max_retry:
                    max_retry = retry
                exhausted = True

        if exhausted:
            return False, max(0.1, max_retry)

        # Phase 2: consume for all pairs
        for bucket, entity_id in pairs:
            _, per = self.get_bucket_cfg(bucket)
            key = (bucket, entity_id)
            cutoff = now - per
            if key not in self._entries:
                self._entries[key] = deque()
            dq = self._entries[key]

            # Prune stale timestamps
            while dq and dq[0] <= cutoff:
                dq.popleft()

            dq.append(now)
            self._last_seen[key] = now
            self._entries.move_to_end(key)

        # LRU eviction
        while len(self._entries) > self.max_keys:
            oldest_key, _ = self._entries.popitem(last=False)
            self._last_seen.pop(oldest_key, None)

        return True, 0.0

    @property
    def _windows(self) -> OrderedDict[tuple[str, int | str], deque[float]]:
        return self._entries

    def acquire_command(
        self,
        user_id: int,
        guild_id: int | None = None,
        command_name: str = "unknown",
        owner_id: int = 0,
    ) -> tuple[bool, float]:
        """Rate limit check for a slash command."""
        if user_id == owner_id:
            return True, 0.0

        pairs: list[tuple[str, int | str]] = []
        if guild_id:
            pairs.append(("guild", guild_id))
        pairs.append(("commands", user_id))

        category = get_command_category(command_name)
        if category is not None:
            pairs.append((category, user_id))

        return self.check_and_acquire(pairs)

    def acquire_component(self, user_id: int, owner_id: int = 0) -> tuple[bool, float]:
        """Rate limit check for a button or select interaction."""
        if user_id == owner_id:
            return True, 0.0
        return self.check_and_acquire([("components", user_id)])

    def acquire_autocomplete(self, user_id: int, owner_id: int = 0) -> tuple[bool, float]:
        """Silent rate limit check for autocomplete interactions."""
        if user_id == owner_id:
            return True, 0.0
        # Autocomplete shares commands bucket or runs 10 per 5s
        return self.check_and_acquire([("commands", user_id)])

    def prune_idle(self, max_idle_seconds: float | None = None, max_idle: float | None = None) -> int:
        """Evict keys that have had no activity within the idle TTL."""
        now = self.clock()
        ttl = self.idle_ttl
        if max_idle_seconds is not None:
            ttl = max_idle_seconds
        elif max_idle is not None:
            ttl = max_idle

        to_remove: list[tuple[str, int | str]] = []

        for key, last in self._last_seen.items():
            if now - last >= ttl:
                to_remove.append(key)

        for key in to_remove:
            self._entries.pop(key, None)
            self._last_seen.pop(key, None)

        return len(to_remove)

    async def cleanup_loop(self) -> None:
        """Periodic background task supervised under MusicBot.supervisor."""
        interval = max(10.0, self.idle_ttl / 2)
        while True:
            await asyncio.sleep(interval)
            try:
                pruned = self.prune_idle()
                if pruned > 0:
                    log.debug("Rate limiter pruned %d idle keys", pruned)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.debug("Rate limit cleanup encountered error: %s", exc)


def extract_qualified_command_name(interaction: discord.Interaction) -> str:
    """Extract full command name including subcommand group and subcommand."""
    cmd = interaction.command
    if cmd is None:
        # Fallback to data name
        data = getattr(interaction, "data", {})
        return str(data.get("name", "unknown")) if isinstance(data, dict) else "unknown"
    return cmd.qualified_name


_DEFAULT_LIMITER: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    """Return the global default RateLimiter instance."""
    global _DEFAULT_LIMITER
    if _DEFAULT_LIMITER is None:
        _DEFAULT_LIMITER = RateLimiter()
    return _DEFAULT_LIMITER
