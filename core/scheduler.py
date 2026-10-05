"""Central timer scheduler using a min-heap of deadlines.

Replaces per-player timer tasks with exactly ONE supervised central loop.
Supports O(1) cancellation per timer or per guild, and deadline rescheduling.
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


class CentralScheduler:
    """Supervised min-heap scheduler for guild player timeouts."""

    def __init__(self, on_expire: Callable[[int, str], Awaitable[None]]) -> None:
        self._on_expire = on_expire
        # Heap elements: (deadline: float, seq: int, guild_id: int, timer_name: str, reason: str)
        self._heap: list[tuple[float, int, int, str, str]] = []
        # Maps (guild_id, timer_name) -> seq of current valid timer
        self._active: dict[tuple[int, str], int] = {}
        self._seq = 0
        self._wakeup_event = asyncio.Event()
        self._stopped = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._stopped = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="central-scheduler")

    def stop(self) -> None:
        self._stopped = True
        self._wakeup_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def schedule(self, guild_id: int, timer_name: str, delay: float, reason: str) -> None:
        """Schedule a timer to expire after delay seconds."""
        if self._stopped:
            return
        self._seq += 1
        seq = self._seq
        self._active[(guild_id, timer_name)] = seq
        deadline = time.monotonic() + delay
        heapq.heappush(self._heap, (deadline, seq, guild_id, timer_name, reason))
        self._wakeup_event.set()

    def cancel(self, guild_id: int, timer_name: str) -> None:
        """Cancel a timer for a guild."""
        if (guild_id, timer_name) in self._active:
            del self._active[(guild_id, timer_name)]
            self._wakeup_event.set()

    def cancel_guild(self, guild_id: int) -> None:
        """Cancel all active timers for a guild."""
        keys = [k for k in self._active if k[0] == guild_id]
        if keys:
            for k in keys:
                del self._active[k]
            self._wakeup_event.set()

    def has_timer(self, guild_id: int, timer_name: str) -> bool:
        """Check if a timer is actively scheduled for a guild."""
        return (guild_id, timer_name) in self._active

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def pending_count(self) -> int:
        return len(self._active)

    async def run(self) -> None:
        """Single central loop dispatching expired deadlines."""
        while not self._stopped:
            try:
                now = time.monotonic()

                # Discard stale heap entries
                while self._heap and self._active.get((self._heap[0][2], self._heap[0][3])) != self._heap[0][1]:
                    heapq.heappop(self._heap)

                if not self._heap:
                    self._wakeup_event.clear()
                    await self._wakeup_event.wait()
                    continue

                deadline, seq, guild_id, timer_name, reason = self._heap[0]
                if deadline <= now:
                    heapq.heappop(self._heap)
                    if self._active.get((guild_id, timer_name)) == seq:
                        del self._active[(guild_id, timer_name)]
                        try:
                            await self._on_expire(guild_id, reason)
                        except Exception:
                            log.exception("Scheduler error expiring guild=%s timer=%s", guild_id, timer_name)
                    continue

                wait_time = max(0.0, deadline - now)
                self._wakeup_event.clear()
                try:
                    await asyncio.wait_for(self._wakeup_event.wait(), timeout=wait_time)
                except (asyncio.TimeoutError, TimeoutError):
                    continue
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("CentralScheduler loop exception; continuing")
                await asyncio.sleep(0.5)
