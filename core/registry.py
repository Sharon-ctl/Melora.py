"""The registry of guild players: shared get-or-create, destroy, sweeps, watchdog.

All per-guild state lives in one dictionary. There is exactly one function
that creates a player (get_or_create) and one that tears it down (destroy).
Every other path, including timers, voice events, sweeps and shutdown, goes
through those two.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from typing import Any

from core.card_flusher import CardFlusher
from core.contracts import PlayerServices
from core.guild_player import GuildPlayer
from core.loop_monitor import LoopLagMonitor
from core.scheduler import CentralScheduler
from utils import messages

log = logging.getLogger(__name__)

LOST_VOICE_NOTICE = messages.voice_disconnected()
LOST_NODE_NOTICE = messages.node_disconnected()


class PlayerRegistry:
    def __init__(self, services: PlayerServices) -> None:
        self.services = services
        self._players: dict[int, GuildPlayer] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self.created_total = 0
        self.destroyed_total = 0

        self.scheduler = CentralScheduler(self.expire)
        self.flusher = CardFlusher()
        self.loop_monitor = LoopLagMonitor(on_load_shedding_changed=self._on_load_shedding_changed)

        if getattr(services, "scheduler", None) is None:
            services.scheduler = self.scheduler
        if getattr(services, "flusher", None) is None:
            services.flusher = self.flusher
        if getattr(services, "loop_monitor", None) is None:
            services.loop_monitor = self.loop_monitor

        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                self.start_background_tasks()
        except RuntimeError:
            log.debug("No running event loop available during registry init")

    def start_background_tasks(self) -> None:
        self.scheduler.start()
        self.flusher.start()
        self.loop_monitor.start()

    def stop_background_tasks(self) -> None:
        self.scheduler.stop()
        self.flusher.stop()
        self.loop_monitor.stop()

    def _on_load_shedding_changed(self, active: bool) -> None:
        if active:
            self.flusher.set_cadence(4.0)
        else:
            self.flusher.reset_cadence()

    @property
    def is_load_shedding(self) -> bool:
        return self.loop_monitor.is_load_shedding

    # ------------------------------------------------------------------ access

    def get(self, guild_id: int) -> GuildPlayer | None:
        player = self._players.get(guild_id)
        if player is not None and player.destroyed:
            return None
        return player

    def __len__(self) -> int:
        return len(self._players)

    def guild_ids(self) -> list[int]:
        return list(self._players)

    def total_queued(self) -> int:
        return sum(len(p.queue) for p in self._players.values())

    def _lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[guild_id] = lock
        return lock

    # ------------------------------------------------------------ get or create

    async def get_or_create(self, guild_id: int, voice_channel_id: int, text_channel_id: int) -> GuildPlayer:
        """Return the guild's healthy player, building a fresh one if needed.

        This is the ghost check that runs before every /play: if a player is
        tracked but its voice connection or Lavalink player is gone, it is
        destroyed and rebuilt.
        """
        backend = self.services.backend
        async with self._lock(guild_id):
            player = self._players.get(guild_id)
            if player is not None and not player.destroyed:
                healthy = backend.voice_connected(guild_id, player.voice_channel_id) and backend.audio(guild_id) is not None
                if healthy:
                    if text_channel_id:
                        player.text_channel_id = text_channel_id
                    return player
                log.warning("guild=%s ghost player found before play; rebuilding", guild_id)
            if player is not None:
                await self._destroy_locked(guild_id, "ghost before play")
            else:
                await backend.purge(guild_id)
            player = GuildPlayer(guild_id, voice_channel_id, text_channel_id, self.services, self.expire)
            storage = getattr(self.services, "storage", None)
            if storage is not None:
                try:
                    s = await storage.get_guild_settings(guild_id)
                    player.is_247 = bool(s.voice_247_channel_id)
                    player.restore_queue_enabled = bool(s.restore_queue)
                    player.autoplay = False
                    player.volume = min(100, max(0, s.default_volume))
                    player.dj_role_id = s.dj_role_id
                    player.dj_only = bool(s.dj_only)
                except Exception as exc:
                    log.debug("guild=%s failed loading settings on player creation: %s", guild_id, exc)
            await backend.connect(guild_id, voice_channel_id)
            player.created_at = time.monotonic()
            self._players[guild_id] = player
            self.created_total += 1
            # Safety net: a player that never gets a track still expires. The timer is
            # cancelled as soon as a track starts.
            if not player.is_247:
                player.start_timer("idle", self.services.cfg.idle_timeout, "idle")
            log.info("guild=%s player created channel=%s", guild_id, voice_channel_id)
            return player

    # ------------------------------------------------------------------ destroy

    async def destroy(self, guild_id: int, reason: str = "requested") -> bool:
        """The single teardown path. Safe to call at any time, any number of times."""
        async with self._lock(guild_id):
            return await self._destroy_locked(guild_id, reason)

    async def _destroy_locked(self, guild_id: int, reason: str) -> bool:
        self.scheduler.cancel_guild(guild_id)
        self.flusher.cancel(guild_id)
        player = self._players.pop(guild_id, None)
        if player is not None:
            # Delete the now playing card before shutting down tasks
            card_ch = player.nowplaying_channel_id
            card_msg = player.nowplaying_message_id
            player.shutdown()
            self.destroyed_total += 1
            if card_ch and card_msg:
                try:
                    await self.services.backend.delete_nowplaying_card(card_ch, card_msg)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.debug("guild=%s card delete on destroy failed", guild_id, exc_info=True)
        try:
            await self.services.backend.purge(guild_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("guild=%s purge failed during destroy", guild_id)
        if player is not None:
            log.info("guild=%s player destroyed reason=%s", guild_id, reason)
        return player is not None

    async def expire(self, guild_id: int, reason: str) -> None:
        """Timer callback for idle, alone, sleep, vote_expiry, and snapshot timeouts."""
        player = self._players.get(guild_id)
        if player is None:
            return
        if reason == "alone":
            humans = self.services.backend.humans_in_channel(guild_id, player.voice_channel_id)
            if humans is not None and humans > 0:
                log.info("guild=%s alone timer fired but members are present; ignoring", guild_id)
                return
            await player.notify(messages.alone_leave())
            await self.destroy(guild_id, reason)
        elif reason == "idle":
            await player.notify(messages.idle_leave())
            await self.destroy(guild_id, reason)
        elif reason == "sleep":
            await player.notify(messages.sleep_finished())
            await self.destroy(guild_id, reason)
        elif reason == "vote_expiry":
            player.votes.clear()
            log.debug("guild=%s vote skip expired; votes cleared", guild_id)
        elif reason == "snapshot":
            await player._debounced_snapshot(0.0)
        else:
            await self.destroy(guild_id, reason)

    async def destroy_all(self, reason: str, chunk_size: int = 50) -> None:
        guild_ids = list(self._players)
        for i in range(0, len(guild_ids), chunk_size):
            chunk = guild_ids[i : i + chunk_size]
            await asyncio.gather(*(self.destroy(g, reason) for g in chunk), return_exceptions=True)
            if i + chunk_size < len(guild_ids):
                await asyncio.sleep(0)

    async def on_guild_removed(self, guild_id: int) -> None:
        await self.destroy(guild_id, "removed from guild")
        self._locks.pop(guild_id, None)
        storage = getattr(self.services, "storage", None)
        if storage is not None:
            try:
                await storage.delete_guild_data(guild_id)
            except Exception as exc:
                log.debug("guild=%s failed deleting storage data on leave: %s", guild_id, exc)

    async def run_guarded(self, guild_id: int, work: Awaitable[None]) -> None:
        """Per-guild error boundary: a failure resets only that guild's player."""
        try:
            await work
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("guild=%s handler failed; resetting this guild's player", guild_id)
            try:
                await self.destroy(guild_id, "internal error")
            except Exception:
                log.exception("guild=%s reset after failure also failed", guild_id)

    # ------------------------------------------------------------------- sweeps

    async def reconcile(self, source: str) -> int:
        """Compare tracked players with real voice and Lavalink state; fix mismatches.

        Used by the watchdog, the startup sweep and the node-reconnect sweep.
        Returns how many problems were fixed.
        """
        backend = self.services.backend
        fixed = 0
        items = list(self._players.items())
        chunk_size = 50
        for i in range(0, len(items), chunk_size):
            chunk = items[i : i + chunk_size]
            for guild_id, player in chunk:
                if self._lock(guild_id).locked():
                    continue
                try:
                    fixed += await self._reconcile_one(guild_id, player, source)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("guild=%s reconcile failed (%s)", guild_id, source)
            if i + chunk_size < len(items):
                await asyncio.sleep(0)

        try:
            strays = list(backend.stray_guild_ids() - set(self._players))
        except Exception:
            log.exception("could not list stray players (%s)", source)
            strays = []

        for i in range(0, len(strays), chunk_size):
            chunk = strays[i : i + chunk_size]
            for guild_id in chunk:
                if self._lock(guild_id).locked():
                    continue
                try:
                    log.warning("guild=%s leftover player or voice connection found (%s)", guild_id, source)
                    await self.destroy(guild_id, f"{source}: leftover")
                    fixed += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("guild=%s leftover cleanup failed (%s)", guild_id, source)
            if i + chunk_size < len(strays):
                await asyncio.sleep(0)

        if fixed:
            log.info("reconcile (%s) fixed %d problem(s)", source, fixed)
        return fixed

    async def _reconcile_one(self, guild_id: int, player: GuildPlayer, source: str) -> int:
        backend = self.services.backend
        cfg = self.services.cfg
        if hasattr(backend, "is_guild_shard_ready") and not backend.is_guild_shard_ready(guild_id):
            log.debug("guild=%s shard is disconnected/reconnecting; skipping reconcile", guild_id)
            return 0
        if player.destroyed:
            self._players.pop(guild_id, None)
            return 1
        if not backend.voice_connected(guild_id, player.voice_channel_id):
            log.warning("guild=%s no voice connection (%s)", guild_id, source)
            await player.notify(LOST_VOICE_NOTICE)
            await self.destroy(guild_id, f"{source}: no voice connection")
            return 1
        if backend.audio(guild_id) is None:
            log.warning("guild=%s no lavalink player (%s)", guild_id, source)
            await player.notify(LOST_VOICE_NOTICE)
            await self.destroy(guild_id, f"{source}: no lavalink player")
            return 1
        if player.current is None and len(player.queue) == 0 and not player.has_timer("idle"):
            player.start_timer("idle", cfg.idle_timeout, "idle")
        humans = backend.humans_in_channel(guild_id, player.voice_channel_id)
        if humans == 0 and not player.has_timer("alone"):
            player.start_timer("alone", cfg.alone_timeout, "alone")
        if player.is_overdue():
            log.warning("guild=%s track ran past its length without an end event (%s)", guild_id, source)
            await player.recover("overdue")
            return 1
        return 0

    async def handle_node_disconnected(self, node: Any) -> None:
        """Handle disconnection of a Lavalink node: ensure failover to healthy node or clean up."""
        backend = self.services.backend
        ideal_node = backend.find_ideal_node(exclude=[node]) if hasattr(backend, "find_ideal_node") else None

        for guild_id, player in list(self._players.items()):
            ll_player = backend.audio(guild_id)
            if ll_player is None:
                continue
            curr_node = getattr(ll_player, "node", None)
            if curr_node == node or (curr_node is not None and not getattr(curr_node, "available", True)):
                if ideal_node is not None:
                    # If lavalink hasn't already switched it to an available node
                    if getattr(curr_node, "name", None) == getattr(node, "name", None):
                        try:
                            log.info(
                                "guild=%s moving player from dead node %s to healthy node %s",
                                guild_id,
                                getattr(node, "name", ""),
                                ideal_node.name,
                            )
                            await ll_player.change_node(ideal_node)
                        except Exception as exc:
                            log.warning("guild=%s failover to node %s failed: %s", guild_id, ideal_node.name, exc)
                            await player.notify(LOST_NODE_NOTICE)
                            await self.destroy(guild_id, f"node loss: failover failed ({exc})")
                else:
                    # No nodes available anywhere; if grace is 0, destroy immediately
                    if getattr(self.services.cfg, "node_loss_grace", 30) <= 0:
                        await player.notify(LOST_NODE_NOTICE)
                        await self.destroy(guild_id, "audio node lost")

    async def handle_all_nodes_down(self) -> int:
        """Called after the node-loss grace period. Destroys players if no node came back."""
        backend = self.services.backend
        if backend.any_node_available():
            return 0
        players = list(self._players.items())
        if not players:
            return 0
        log.error("No audio node available after grace period; destroying %d player(s)", len(players))

        async def drop(guild_id: int, player: GuildPlayer) -> None:
            await player.notify(LOST_NODE_NOTICE)
            await self.destroy(guild_id, "audio node lost")

        await asyncio.gather(*(drop(g, p) for g, p in players), return_exceptions=True)
        return len(players)

    # ----------------------------------------------------------------- watchdog

    async def watchdog(self) -> int:
        """Run a single watchdog reconciliation pass."""
        return await self.reconcile("watchdog")

    async def watchdog_loop(self) -> None:
        interval = self.services.cfg.watchdog_interval
        while True:
            await asyncio.sleep(interval)
            try:
                if self.is_load_shedding:
                    log.debug("Deferring non-critical watchdog pass: load shedding active")
                    continue
                await asyncio.wait_for(self.reconcile("watchdog"), timeout=max(interval * 2, 60))
                self._prune_locks()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("watchdog pass failed; continuing")

    def _prune_locks(self) -> None:
        """Forget locks for guilds the bot is no longer in."""
        known = self.services.backend.guild_ids()
        for guild_id in [g for g in self._locks if g not in known and g not in self._players]:
            lock = self._locks.get(guild_id)
            if lock is not None and not lock.locked():
                del self._locks[guild_id]
