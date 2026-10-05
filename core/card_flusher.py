"""Central flusher for now-playing card edits.

Replaces per-guild card edit tasks with a single flusher loop running at a steady
cadence. Guarantees latest-wins per guild, adapts cadence during load shedding,
and holds zero persistent tasks while guilds are idle.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class CardFlusher:
    """Single background task flushing queued card updates."""

    def __init__(self, default_cadence: float = 1.0) -> None:
        self._default_cadence = default_cadence
        self._current_cadence = default_cadence
        # Maps guild_id -> player
        self._pending: dict[int, Any] = {}
        self._stopped = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._stopped = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="card-flusher")

    def stop(self) -> None:
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def schedule(self, guild_id: int, player: Any) -> None:
        """Enqueue or replace pending card update for a guild (latest-wins)."""
        if not self._stopped:
            self._pending[guild_id] = player

    def cancel(self, guild_id: int) -> None:
        """Cancel any pending card update for a guild."""
        self._pending.pop(guild_id, None)

    def cancel_guild(self, guild_id: int) -> None:
        """Cancel any pending card update for a guild."""
        self._pending.pop(guild_id, None)

    def set_cadence(self, cadence: float) -> None:
        self._current_cadence = max(0.2, cadence)

    def reset_cadence(self) -> None:
        self._current_cadence = self._default_cadence

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def run(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self._current_cadence)
                if not self._pending:
                    continue

                batch = self._pending
                self._pending = {}

                for guild_id, player in batch.items():
                    if getattr(player, "destroyed", False):
                        continue
                    try:
                        await player._update_card_locked()
                    except Exception:
                        log.debug("guild=%s flusher card update failed", guild_id, exc_info=True)
                    # Yield between edits so event loop stays responsive
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("CardFlusher loop error; continuing")
                await asyncio.sleep(1.0)
