"""State and playback logic for one guild.

A GuildPlayer holds only IDs and plain values, never Discord objects, so a
destroyed player can be garbage collected immediately. All its tasks live in
its own TaskSet and are cancelled when it is shut down.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from lavalink.filters import Equalizer, Timescale

from core.contracts import AudioPlayer, PlayerServices
from core.filters import get_filter_types_for_preset, validate_and_build_eq, validate_and_build_filters
from core.matching import SpotifyResolver
from core.queue import LoopMode, QueueItem, TrackQueue, validate_track
from core.storage import StoredTrack
from utils import messages
from utils.errors import BotUserError, NodeOffline, NothingPlaying, QueueFull, TrackTooLong, UserLimitReached
from utils.supervisor import TaskSet
from utils.text import clean_track_title, format_duration, truncate

log = logging.getLogger(__name__)

AUDIO_TIMEOUT = 10.0
OVERDUE_GRACE_SECONDS = 180.0
ExpireCallback = Callable[[int, str], Awaitable[None]]
_GLOBAL_PREFETCH_SEMAPHORE = asyncio.Semaphore(16)


@dataclass(frozen=True)
class EnqueueResult:
    added: int
    skipped: int
    position: int  # 1-based place among upcoming tracks, 0 if it started immediately
    started: bool


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError as exc:
        log.debug("_current_task query failed: %s", exc)
        return None


class GuildPlayer:
    def __init__(
        self,
        guild_id: int,
        voice_channel_id: int,
        text_channel_id: int,
        services: PlayerServices,
        expire_callback: ExpireCallback | None = None,
    ) -> None:
        self.guild_id = guild_id
        self.voice_channel_id = voice_channel_id
        self.text_channel_id = text_channel_id
        self.services = services
        self.cfg = services.cfg
        self.queue = TrackQueue(self.cfg.max_queue_size, self.cfg.max_per_user, getattr(self.cfg, "history_size", 50))
        self.current: QueueItem | None = None
        self.paused = False
        self.volume = self.cfg.default_volume
        self.failures = 0
        self.destroyed = False
        self.votes: set[int] = set()
        self.autoplay = False
        self.autoplay_failures = 0
        self.lock = asyncio.Lock()
        self.tasks = TaskSet(f"guild-{guild_id}", max_tasks=32)
        self.created_at = time.monotonic()
        self.started_at = 0.0
        self.paused_total = 0.0
        self.paused_since: float | None = None
        self._timers: dict[str, asyncio.Task[Any]] = {}
        self._end_handled = True
        self._expire_callback = expire_callback
        self.spotify_resolver = SpotifyResolver()
        self.is_247 = False
        self.restore_queue_enabled = False
        self.applied_filters: set[str] = set()
        self.current_eq: str | None = None
        self.dj_role_id: int = 0
        self.dj_only: bool = False
        self.smooth_playback: bool = True
        # Now playing card state (IDs only, never Discord objects)
        self.nowplaying_channel_id: int | None = None
        self.nowplaying_message_id: int | None = None
        self.nowplaying_view: Any = None
        self._card_lock = asyncio.Lock()
        self._last_card_edit: float = 0.0
        self._card_coalesce_task: asyncio.Task[Any] | None = None
        self._prefetch_task: asyncio.Task[Any] | None = None

    @property
    def _last_card_message_id(self) -> int | None:
        """Alias for nowplaying_message_id for component guard consistency."""
        return self.nowplaying_message_id

    def cancel_prefetch(self) -> None:
        """Cancel any running background prefetch task."""
        if self._prefetch_task is not None:
            if not self._prefetch_task.done():
                self._prefetch_task.cancel()
            self._prefetch_task = None

    # ------------------------------------------------------------------ helpers

    def age(self) -> float:
        return time.monotonic() - self.created_at

    @property
    def audio(self) -> AudioPlayer | None:
        return self.services.backend.audio(self.guild_id)

    def _audio_or_raise(self) -> AudioPlayer:
        audio = self.audio
        if audio is None:
            raise NodeOffline("There is no active audio player. Try /play again.")
        return audio

    async def _audio_call(self, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            await asyncio.wait_for(coro, timeout=AUDIO_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("guild=%s audio call timed out", self.guild_id)
            raise NodeOffline("The audio server did not respond in time.") from None
        except BotUserError:
            raise
        except Exception as exc:
            log.warning("guild=%s audio call failed: %s", self.guild_id, type(exc).__name__)
            raise NodeOffline("The audio server is not responding.") from None

    async def notify(self, text: str) -> None:
        """Send a short notice to the text channel. Quiet if it is not possible."""
        if self.destroyed or not self.text_channel_id:
            return
        try:
            await self.services.backend.notify(self.text_channel_id, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("guild=%s notice failed", self.guild_id, exc_info=True)

    def notify_soon(self, text: str) -> None:
        self.tasks.spawn(self.notify(text), name="notify")

    # --------------------------------------------------------------- card lifecycle

    def schedule_card_update(self) -> None:
        """Schedule a coalesced card update (latest-wins via central flusher)."""
        if self.destroyed or self.nowplaying_channel_id is None:
            return
        flusher = getattr(self.services, "flusher", None)
        if flusher is not None:
            flusher.schedule(self.guild_id, self)
            return
        if self._card_coalesce_task is not None and not self._card_coalesce_task.done():
            return  # already pending
        task = self.tasks.spawn(self._coalesced_card_update(), name="card-update")
        if task is not None:
            self._card_coalesce_task = task

    async def _coalesced_card_update(self) -> None:
        """Wait until at least 1s since last edit, then push the update."""
        now = time.monotonic()
        wait = max(0.0, 1.0 - (now - self._last_card_edit))
        if wait > 0:
            await asyncio.sleep(wait)
        self._card_coalesce_task = None
        if self.destroyed:
            return
        await self._update_card_locked()

    async def _update_card_locked(self, *, recreate: bool = False) -> None:
        """Render and send/edit the now playing card under the card lock."""
        async with self._card_lock:
            if self.destroyed or self.current is None:
                return
            from utils.components_v2 import NowPlayingView

            backend = self.services.backend
            ch_id = self.nowplaying_channel_id or self.text_channel_id
            msg_id = self.nowplaying_message_id

            # If we have an existing card and are not recreating, try edit
            if msg_id is not None and ch_id and not recreate:
                try:
                    if self.nowplaying_view is None:
                        self.nowplaying_view = NowPlayingView(self)
                    container = self.nowplaying_view.render(is_paused=self.paused, has_history=self.queue.has_history)
                    ok = await backend.edit_nowplaying_card(ch_id, msg_id, container, self.nowplaying_view)
                    if ok:
                        self._last_card_edit = time.monotonic()
                        return
                except Exception:
                    # NotFound/Forbidden -> try recreate once
                    log.debug("guild=%s card edit failed, will recreate", self.guild_id)

            # Send a new card
            if self.destroyed or self.current is None:
                return
            if self.nowplaying_view is not None:
                self.nowplaying_view.stop()
            self.nowplaying_view = NowPlayingView(self)
            container = self.nowplaying_view.render(is_paused=self.paused, has_history=self.queue.has_history)
            new_msg_id = await backend.send_nowplaying_card(ch_id, container, self.nowplaying_view)
            if new_msg_id is not None:
                if self.destroyed or self.current is None:
                    try:
                        await backend.delete_nowplaying_card(ch_id, new_msg_id)
                    except Exception as del_exc:
                        log.debug("guild=%s failed deleting orphaned card: %s", self.guild_id, del_exc)
                    return
                self.nowplaying_channel_id = ch_id
                self.nowplaying_message_id = new_msg_id
                self._last_card_edit = time.monotonic()
            else:
                log.info("guild=%s failed to send now playing card", self.guild_id)
                self.nowplaying_channel_id = None
                self.nowplaying_message_id = None
                self.nowplaying_view.stop()
                self.nowplaying_view = None

    async def create_card(self) -> None:
        """Create the now playing card for the first track."""
        if self.destroyed or self.current is None:
            return
        self.nowplaying_channel_id = self.text_channel_id
        self.nowplaying_message_id = None
        await self._update_card_locked(recreate=True)

    async def delete_card(self) -> None:
        """Delete the now playing card message. Safe to call any time."""
        flusher = getattr(self.services, "flusher", None)
        if flusher is not None:
            flusher.cancel(self.guild_id)
        if self._card_coalesce_task is not None and not self._card_coalesce_task.done():
            self._card_coalesce_task.cancel()
            self._card_coalesce_task = None
        async with self._card_lock:
            ch_id = self.nowplaying_channel_id
            msg_id = self.nowplaying_message_id
            self.nowplaying_channel_id = None
            self.nowplaying_message_id = None
            if self.nowplaying_view is not None:
                self.nowplaying_view.stop()
                self.nowplaying_view = None
            if ch_id and msg_id:
                try:
                    await self.services.backend.delete_nowplaying_card(ch_id, msg_id)
                except Exception as exc:
                    log.debug("guild=%s card delete failed: %s", self.guild_id, exc)

    async def move_card(self, new_channel_id: int) -> None:
        """Move the card to a new channel (delete old, send new)."""
        await self.delete_card()
        self.nowplaying_channel_id = new_channel_id
        await self._update_card_locked(recreate=True)

    def schedule_snapshot(self) -> None:
        """Debounce writing a queue snapshot to storage (delay grows with queue size)."""
        if not self.restore_queue_enabled:
            return
        self.cancel_timer("snapshot")
        delay = min(30.0, 5.0 + (len(self.queue) / 5000.0))
        task = self.tasks.spawn(self._debounced_snapshot(delay), name="timer-snapshot")
        if task is not None:
            self._timers["snapshot"] = task

    async def save_snapshot_now(self) -> None:
        """Immediately save queue snapshot to storage without debouncing."""
        storage = getattr(self.services, "storage", None)
        if storage is None:
            return
        tracks: list[StoredTrack] = []
        if self.current is not None:
            tracks.append(
                StoredTrack(
                    uri=self.current.uri,
                    title=self.current.title,
                    artist=self.current.artist,
                    duration_ms=self.current.duration_ms,
                    requester_id=self.current.requester_id,
                )
            )
        for item in self.queue:
            tracks.append(
                StoredTrack(
                    uri=item.uri,
                    title=item.title,
                    artist=item.artist,
                    duration_ms=item.duration_ms,
                    requester_id=item.requester_id,
                )
            )
        if tracks:
            try:
                await storage.save_queue_snapshot(self.guild_id, tracks)
                log.info("guild=%s saved queue snapshot (%d tracks)", self.guild_id, len(tracks))
            except Exception as exc:
                log.debug("guild=%s failed to save queue snapshot: %s", self.guild_id, exc)

    async def _debounced_snapshot(self, delay: float = 5.0) -> None:
        await asyncio.sleep(delay)
        self._timers.pop("snapshot", None)
        if self.destroyed:
            return
        await self.save_snapshot_now()

    # ------------------------------------------------------------------- timers

    def has_timer(self, name: str) -> bool:
        scheduler = getattr(self.services, "scheduler", None)
        if scheduler is not None:
            return scheduler.has_timer(self.guild_id, name)
        task = self._timers.get(name)
        return task is not None and not task.done()

    def start_timer(self, name: str, delay: float, reason: str) -> None:
        self.cancel_timer(name)
        if self.destroyed or (self.is_247 and reason in ("idle", "alone")):
            return
        scheduler = getattr(self.services, "scheduler", None)
        if scheduler is not None:
            scheduler.schedule(self.guild_id, name, delay, reason)
            return
        task = self.tasks.spawn(self._timer_body(name, delay, reason), name=f"timer-{name}")
        if task is not None:
            self._timers[name] = task

    def cancel_timer(self, name: str) -> None:
        scheduler = getattr(self.services, "scheduler", None)
        if scheduler is not None:
            scheduler.cancel(self.guild_id, name)
        task = self._timers.pop(name, None)
        if task is not None and not task.done() and task is not _current_task():
            task.cancel()

    def cancel_all_timers(self) -> None:
        scheduler = getattr(self.services, "scheduler", None)
        if scheduler is not None:
            scheduler.cancel_guild(self.guild_id)
        for name in list(self._timers):
            self.cancel_timer(name)

    async def _timer_body(self, name: str, delay: float, reason: str) -> None:
        await asyncio.sleep(delay)
        self._timers.pop(name, None)
        await self._expire(reason)

    async def _expire(self, reason: str) -> None:
        callback = self._expire_callback
        if callback is not None and not self.destroyed:
            await callback(self.guild_id, reason)

    async def _expire_with_notice(self, reason: str, notice: str | None) -> None:
        if notice:
            await self.notify(notice)
        await self._expire(reason)

    # ----------------------------------------------------------- playback core

    def _mark_started(self) -> None:
        self.started_at = time.monotonic()
        self.paused_total = 0.0
        self.paused_since = None
        self.paused = False
        self._schedule_preload_next()
        vs = getattr(self.services, "voice_status", None)
        if vs is not None:
            vs.on_track_start(self.guild_id, self)

    def is_overdue(self) -> bool:
        """True if the current track has run far longer than its length (a missed end event)."""
        item = self.current
        if item is None or self.paused or item.is_stream or self.started_at <= 0:
            return False
        elapsed = time.monotonic() - self.started_at - self.paused_total
        return elapsed > item.duration_ms / 1000.0 + OVERDUE_GRACE_SECONDS

    async def _stop_audio_locked(self) -> None:
        audio = self.audio
        if audio is None:
            return
        try:
            await self._audio_call(audio.stop())
        except NodeOffline:
            log.info("guild=%s could not stop audio (server unreachable)", self.guild_id)
        try:
            audio.current = None
        except Exception as exc:
            log.debug("guild=%s could not clear audio.current: %s", self.guild_id, exc)

    async def _try_autoplay_locked(self, last_track: QueueItem | None) -> bool:
        if not self.autoplay or self.autoplay_failures >= 3:
            return False
        search_query = ""
        if last_track is not None:
            search_query = last_track.artist or last_track.title
        if not search_query:
            return False
        try:
            outcome = await self.services.loader.load(self.guild_id, search_query)
            seen = {clean_track_title(h.title).lower() for h in self.queue.get_history(50)}
            if last_track:
                seen.add(clean_track_title(last_track.title).lower())
            for item in self.queue:
                seen.add(clean_track_title(item.title).lower())

            added_items: list[QueueItem] = []
            bot_avatar = self.services.backend.bot_avatar_url() if hasattr(self.services.backend, "bot_avatar_url") else None
            bot_id = self.services.backend.bot_id() if hasattr(self.services.backend, "bot_id") else 0
            requester_id = last_track.requester_id if last_track and last_track.requester_id else bot_id
            last_avatar = last_track.requester_avatar_url if last_track else None
            requester_avatar = last_avatar or bot_avatar
            for t in outcome.tracks:
                t_title = clean_track_title(getattr(t, "title", "") or "")
                if t_title.lower() not in seen:
                    seen.add(t_title.lower())
                    try:
                        validate_track(t, self.cfg.max_track_seconds)
                        added_items.append(QueueItem.from_track(t, requester_id, requester_avatar_url=requester_avatar))
                    except TrackTooLong:
                        continue
                if len(added_items) >= getattr(self.cfg, "autoplay_batch_size", 5):
                    break

            if added_items:
                res = self.queue.add_many(added_items)
                if res.added > 0:
                    self.autoplay_failures = 0
                    next_item = self.queue.next_item(None)
                    if next_item is not None:
                        return await self._play_item_locked(next_item)
            self.autoplay_failures += 1
        except Exception as exc:
            log.debug("guild=%s autoplay query failed: %s", self.guild_id, exc)
            self.autoplay_failures += 1
        return False

    async def _advance_locked(self, *, skipped: bool = False, failed: bool = False) -> bool:
        """Move to the next track. Returns True if a track was started."""
        self.votes.clear()
        previous = self.current
        self.current = None
        item = self.queue.next_item(previous, skipped=skipped, failed=failed)
        if item is None:
            if not failed and await self._try_autoplay_locked(previous):
                return True
            self._end_handled = True
            await self._stop_audio_locked()
            self.start_timer("idle", self.cfg.idle_timeout, "idle")
            self.schedule_snapshot()
            self.tasks.spawn(self.delete_card(), name="card-delete")
            vs = getattr(self.services, "voice_status", None)
            if vs is not None:
                vs.on_queue_ended(self.guild_id, self)
            return False
        started = await self._play_item_locked(item)
        self.schedule_snapshot()
        return started

    async def _play_item_locked(self, item: QueueItem) -> bool:
        backend = self.services.backend
        audio = backend.audio(self.guild_id)
        if audio is None or not backend.voice_connected(self.guild_id, self.voice_channel_id):
            log.warning("guild=%s ghost player detected before play", self.guild_id)
            self.current = None
            self._end_handled = True
            self.tasks.spawn(
                self._expire_with_notice("ghost", "Lost the voice connection. Playback stopped."),
                name="expire-ghost",
            )
            return False
        self.current = item
        self._end_handled = False
        self.cancel_timer("idle")

        # Lazy resolution for tracks without audio object (Spotify or saved items)
        if item.track is None:
            if item.spotify_metadata:
                meta = item.spotify_metadata
                target_artists = meta.get("artists") or ([item.artist] if item.artist else [])
                resolved = await self.spotify_resolver.resolve(
                    target_title=meta.get("title", item.title),
                    target_artists=target_artists,
                    target_duration_ms=meta.get("duration_ms", item.duration_ms),
                    spotify_uri=meta.get("uri", item.uri),
                    loader=self.services.loader,
                    guild_id=self.guild_id,
                )
                if resolved is None:
                    log.info("guild=%s no matching candidate for Spotify track: %s", self.guild_id, item.title)
                    await self.services.backend.notify(
                        self.text_channel_id,
                        messages.spotify_match_failed(truncate(item.title)),
                    )
                    return await self._advance_locked(skipped=True)
                item.replace_track(resolved)
                self._record_resolved_history(item)
            elif item.uri or item.query:
                load_target = item.uri or item.query or ""
                try:
                    outcome = await self.services.loader.load(self.guild_id, load_target)
                    if outcome.tracks:
                        item.replace_track(outcome.tracks[0])
                    else:
                        log.info("guild=%s no tracks found loading saved item: %s", self.guild_id, item.title)
                        return await self._handle_failure_locked(item, "no tracks found")
                except Exception as exc:
                    log.info("guild=%s failed loading saved item (%s): %s", self.guild_id, load_target, exc)
                    return await self._handle_failure_locked(item, "load failed")

        try:
            await self._preprocess_audio_locked(audio)
            await self._audio_call(audio.play(item.track, volume=self.volume, pause=False))
        except NodeOffline:
            return await self._handle_failure_locked(item, "start failed")
        self._mark_started()
        return True

    async def _preprocess_audio_locked(self, audio: Any) -> None:
        """Apply audio pre-processing and smooth playback optimizations before playback."""
        self.volume = max(0, min(100, self.volume))
        if self.smooth_playback and not any(
            f in self.applied_filters for f in ("nightcore", "vaporwave", "smooth", "speed")
        ):
            try:
                ts = Timescale()
                ts.update(speed=0.98, pitch=1.0, rate=1.0)
                await self._audio_call(audio.set_filter(ts))
            except Exception as exc:
                log.debug("guild=%s audio preprocessing filter failed: %s", self.guild_id, exc)

    def _schedule_preload_next(self) -> None:
        """Spawn background task to pre-resolve the next 2 queued items for instant transitions."""
        self.cancel_prefetch()
        if self.destroyed or len(self.queue) == 0:
            return
        task = self.tasks.spawn(self._preload_next(), name="preload-next")
        if task is not None:
            self._prefetch_task = task
        else:
            log.debug("guild=%s could not spawn preload-next", self.guild_id)

    async def _resolve_one_item(self, item: QueueItem) -> None:
        if item.track is not None or self.destroyed:
            return
        try:
            if item.spotify_metadata:
                meta = item.spotify_metadata
                target_artists = meta.get("artists") or ([item.artist] if item.artist else [])
                resolved = await self.spotify_resolver.resolve(
                    target_title=meta.get("title", item.title),
                    target_artists=target_artists,
                    target_duration_ms=meta.get("duration_ms", item.duration_ms),
                    spotify_uri=meta.get("uri", item.uri),
                    loader=self.services.loader,
                    guild_id=self.guild_id,
                )
                if resolved is not None and item.track is None:
                    item.replace_track(resolved)
                    self._record_resolved_history(item)
            elif item.uri or item.query:
                load_target = item.uri or item.query or ""
                outcome = await self.services.loader.load(self.guild_id, load_target)
                if outcome.tracks and item.track is None:
                    item.replace_track(outcome.tracks[0])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("guild=%s preload next track failed: %s", self.guild_id, exc)

    def _record_resolved_history(self, item: Any) -> None:
        if getattr(item, "from_play_command", False) and self.services.storage:
            item.from_play_command = False
            cfg = self.services.cfg
            if getattr(cfg, "user_history_enabled", True):
                max_h = getattr(cfg, "user_history_max", 50)
                asyncio.create_task(
                    self.services.storage.record_user_play_history(
                        item.requester_id,
                        item.title,
                        item.artist or "",
                        item.uri or "",
                        max_entries=max_h,
                    )
                )

    async def _preload_next(self) -> None:
        """Pre-fetch and resolve up to 2 upcoming tracks in background using asyncio.gather."""
        if self.destroyed or len(self.queue) == 0:
            return
        unresolved: list[QueueItem] = []
        for item in self.queue:
            if item.track is None:
                unresolved.append(item)
                if len(unresolved) >= 2:
                    break
        if not unresolved:
            return

        async with _GLOBAL_PREFETCH_SEMAPHORE:
            tasks = [self._resolve_one_item(item) for item in unresolved]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _load_fallback(self, item: QueueItem) -> Any | None:
        try:
            track = await self.services.loader.load_first_with_source(
                self.guild_id, self.cfg.fallback_search_source, item.query or ""
            )
            validate_track(track, self.cfg.max_track_seconds)
            return track
        except asyncio.CancelledError:
            raise
        except BotUserError as exc:
            desc = "load failed" if "LoadFailed" in type(exc).__name__ else "no matches"
            log.info("guild=%s fallback search failed: %s: %s", self.guild_id, type(exc).__name__, desc)
        except Exception:
            log.exception("guild=%s fallback search crashed", self.guild_id)
        return None

    async def _handle_failure_locked(self, item: QueueItem, why: str) -> bool:
        """A track failed. Try the fallback source once, then skip, with a circuit breaker."""
        self.current = None
        self._end_handled = True
        log.warning("guild=%s track failed (%s): %s", self.guild_id, why, truncate(item.title, 80))
        cfg = self.cfg
        if (
            item.query
            and not item.fallback_used
            and cfg.fallback_search_source != cfg.default_search_source
        ):
            item.fallback_used = True
            replacement = await self._load_fallback(item)
            if replacement is not None:
                item.replace_track(replacement)
                return await self._play_item_locked(item)
        self.failures += 1
        if self.failures >= cfg.failure_breaker:
            count = self.failures
            self.failures = 0
            self.queue.clear()
            await self._stop_audio_locked()
            self.start_timer("idle", cfg.idle_timeout, "idle")
            self.notify_soon(messages.circuit_breaker_tripped_count(count))
            return False
        reason = "Skipping." if len(self.queue) else None
        self.notify_soon(messages.track_failed_notice(truncate(item.title, 60), reason=reason))
        return await self._advance_locked(failed=True)

    # ---------------------------------------------------------- lavalink events

    def _same_track(self, encoded: str | None) -> bool:
        if encoded is None or self.current is None:
            return True
        return getattr(self.current.track, "track", None) == encoded

    async def on_track_start(self) -> None:
        self.failures = 0
        self.cancel_timer("idle")
        # Create or update the now playing card
        if self.nowplaying_message_id is None:
            self.tasks.spawn(self.create_card(), name="card-create")
        else:
            self.schedule_card_update()

    async def on_track_end(self, encoded: str | None, reason: str) -> None:
        if reason not in ("finished", "loadFailed"):
            return
        async with self.lock:
            if self.destroyed or self.current is None or self._end_handled or not self._same_track(encoded):
                return
            self._end_handled = True
            if reason == "loadFailed":
                await self._handle_failure_locked(self.current, "load failed")
            else:
                await self._advance_locked()

    async def on_track_exception(self, encoded: str | None, message: str | None) -> None:
        log.warning("guild=%s track exception: %s", self.guild_id, truncate(message or "unknown", 120))
        await self._fail_current(encoded, "exception")

    async def on_track_stuck(self, encoded: str | None) -> None:
        log.warning("guild=%s track stuck", self.guild_id)
        await self._fail_current(encoded, "stuck")

    async def _fail_current(self, encoded: str | None, why: str) -> None:
        async with self.lock:
            if self.destroyed or self.current is None or self._end_handled or not self._same_track(encoded):
                return
            self._end_handled = True
            await self._handle_failure_locked(self.current, why)

    async def recover(self, why: str) -> None:
        """Called by the watchdog when the current track clearly ended without an event."""
        async with self.lock:
            if self.destroyed or self.current is None:
                return
            self._end_handled = True
            await self._handle_failure_locked(self.current, why)

    # ---------------------------------------------------------------- commands

    async def enqueue(self, items: list[QueueItem]) -> EnqueueResult:
        async with self.lock:
            if self.destroyed:
                raise BotUserError("The player was closed. Try again.")
            self.cancel_prefetch()
            before = len(self.queue)
            result = self.queue.add_many(items)
            if result.added == 0:
                if result.reason == "user":
                    raise UserLimitReached(self.cfg.max_per_user)
                raise QueueFull()
            position = before + 1 if self.current is not None else 0
            started = False
            if self.current is None:
                started = await self._advance_locked()
                position = 0
            else:
                self._schedule_preload_next()
            self.schedule_snapshot()
            return EnqueueResult(result.added, result.skipped, position, started)

    async def skip(self) -> QueueItem:
        async with self.lock:
            if self.destroyed:
                raise BotUserError("The player was closed. Try again.")
            item = self.current
            if item is None:
                raise NothingPlaying()
            self.cancel_prefetch()
            self._end_handled = True
            await self._advance_locked(skipped=True)
            return item

    async def stop(self) -> None:
        async with self.lock:
            self.queue.clear()
            self.current = None
            self._end_handled = True
            self.paused = False
            await self._stop_audio_locked()
            self.start_timer("idle", self.cfg.idle_timeout, "idle")
            self.schedule_snapshot()
            vs = getattr(self.services, "voice_status", None)
            if vs is not None:
                vs.on_queue_ended(self.guild_id, self)
        await self.delete_card()

    async def pause(self) -> None:
        async with self.lock:
            if self.destroyed:
                raise BotUserError("The player was closed. Try again.")
            if self.current is None:
                raise NothingPlaying()
            if self.paused:
                raise BotUserError("Already paused.")
            await self._audio_call(self._audio_or_raise().set_pause(True))
            self.paused = True
            self.paused_since = time.monotonic()
            self.start_timer("idle", self.cfg.idle_timeout, "idle")
        self.schedule_card_update()
        vs = getattr(self.services, "voice_status", None)
        if vs is not None:
            vs.on_pause(self.guild_id, self)

    async def resume(self) -> None:
        async with self.lock:
            if self.destroyed:
                raise BotUserError("The player was closed. Try again.")
            if self.current is None:
                raise NothingPlaying()
            if not self.paused:
                raise BotUserError("Not paused.")
            await self._audio_call(self._audio_or_raise().set_pause(False))
            self.paused = False
            if self.paused_since is not None:
                self.paused_total += time.monotonic() - self.paused_since
                self.paused_since = None
            self.cancel_timer("idle")
        self.schedule_card_update()
        vs = getattr(self.services, "voice_status", None)
        if vs is not None:
            vs.on_resume(self.guild_id, self)

    async def set_volume(self, level: int) -> int:
        async with self.lock:
            clamped = max(0, min(100, level))
            self.volume = clamped
            audio = self.audio
            if audio is not None:
                await self._audio_call(audio.set_volume(clamped))
            return clamped

    async def clear(self) -> int:
        async with self.lock:
            self.cancel_prefetch()
            count = self.queue.clear()
            self.schedule_snapshot()
            return count

    async def shuffle(self) -> None:
        async with self.lock:
            if len(self.queue) < 2:
                raise BotUserError("There are not enough tracks to shuffle.")
            self.cancel_prefetch()
            self.queue.shuffle()
            self._schedule_preload_next()
            self.schedule_snapshot()

    async def insert(self, item: QueueItem, position: int = 1) -> int:
        async with self.lock:
            if self.destroyed:
                raise BotUserError("The player was closed. Try again.")
            if self.current is None:
                started = await self._play_item_locked(item)
                self.schedule_snapshot()
                return 0 if started else 1
            pos = self.queue.insert(position, item)
            self._schedule_preload_next()
            self.schedule_snapshot()
            return pos

    async def play_instant(self, item: QueueItem) -> bool:
        async with self.lock:
            if self.destroyed:
                raise BotUserError("The player was closed. Try again.")
            self._end_handled = True
            await self._stop_audio_locked()
            started = await self._play_item_locked(item)
            self.schedule_snapshot()
            return started

    async def replay(self) -> None:
        async with self.lock:
            if self.destroyed:
                raise BotUserError("The player was closed. Try again.")
            if self.current is None:
                raise NothingPlaying()
            if self.current.is_stream:
                raise BotUserError("Live streams cannot be seeked.")
            audio = self._audio_or_raise()
            await self._audio_call(audio.seek(0))
            self._mark_started()

    async def previous(self) -> QueueItem:
        async with self.lock:
            if self.destroyed:
                raise BotUserError("The player was closed. Try again.")
            prev = self.queue.pop_history()
            if prev is None:
                raise BotUserError("No previous tracks in history.")
            if self.current is not None:
                self.queue.push_front(self.current)
            self.current = None
            self._end_handled = True
            await self._play_item_locked(prev)
            self.schedule_snapshot()
            return prev

    def _current_position_seconds(self) -> int:
        audio = self.audio
        if audio is not None and getattr(audio, "position", 0) > 0:
            return int(audio.position // 1000)
        if self.started_at > 0:
            elapsed = time.monotonic() - self.started_at - self.paused_total
            return max(0, int(elapsed))
        return 0

    async def seek(self, seconds: int) -> None:
        async with self.lock:
            if self.destroyed:
                raise BotUserError("The player was closed. Try again.")
            if self.current is None:
                raise NothingPlaying()
            if self.current.is_stream:
                raise BotUserError("Live streams cannot be seeked.")
            duration_s = self.current.duration_ms // 1000
            if seconds > duration_s:
                raise BotUserError(f"Position exceeds track duration of {format_duration(self.current.duration_ms)}.")
            target_ms = max(0, seconds * 1000)
            audio = self._audio_or_raise()
            await self._audio_call(audio.seek(target_ms))
            self.started_at = time.monotonic() - (target_ms / 1000.0)
            self.paused_total = 0.0

    async def forward(self, seconds: int) -> int:
        async with self.lock:
            if self.current is None:
                raise NothingPlaying()
            if self.current.is_stream:
                raise BotUserError("Live streams cannot be seeked.")
            curr = self._current_position_seconds()
            target_s = min(curr + seconds, self.current.duration_ms // 1000)
            target_ms = target_s * 1000
            audio = self._audio_or_raise()
            await self._audio_call(audio.seek(target_ms))
            self.started_at = time.monotonic() - (target_ms / 1000.0)
            self.paused_total = 0.0
            return target_s

    async def rewind(self, seconds: int) -> int:
        async with self.lock:
            if self.current is None:
                raise NothingPlaying()
            if self.current.is_stream:
                raise BotUserError("Live streams cannot be seeked.")
            curr = self._current_position_seconds()
            target_s = max(0, curr - seconds)
            target_ms = target_s * 1000
            audio = self._audio_or_raise()
            await self._audio_call(audio.seek(target_ms))
            self.started_at = time.monotonic() - (target_ms / 1000.0)
            self.paused_total = 0.0
            return target_s

    async def skipto(self, position: int) -> tuple[QueueItem, list[QueueItem]]:
        async with self.lock:
            if self.current is None:
                raise NothingPlaying()
            try:
                dropped = self.queue.skipto(position)
            except IndexError:
                raise BotUserError("Invalid position in queue.") from None
            self._end_handled = True
            await self._advance_locked(skipped=True)
            assert self.current is not None
            self.schedule_snapshot()
            return self.current, dropped

    async def move(self, from_pos: int, to_pos: int) -> QueueItem:
        async with self.lock:
            try:
                item = self.queue.move(from_pos, to_pos)
                self.schedule_snapshot()
                return item
            except IndexError:
                raise BotUserError("Invalid position in queue.") from None

    async def swap(self, pos1: int, pos2: int) -> tuple[QueueItem, QueueItem]:
        async with self.lock:
            try:
                pair = self.queue.swap(pos1, pos2)
                self.schedule_snapshot()
                return pair
            except IndexError:
                raise BotUserError("Invalid position in queue.") from None

    async def dedupe(self) -> int:
        async with self.lock:
            count = self.queue.dedupe()
            if count > 0:
                self.schedule_snapshot()
            return count

    async def remove_many(self, position: int, count: int = 1) -> list[QueueItem]:
        async with self.lock:
            try:
                items = self.queue.remove_many(position, count)
                self.schedule_snapshot()
                return items
            except IndexError:
                raise BotUserError("There is no track at that position.") from None

    async def remove(self, position: int) -> QueueItem:
        items = await self.remove_many(position, 1)
        return items[0]

    async def vote_skip(self, user_id: int, humans_count: int) -> tuple[bool, int, int]:
        async with self.lock:
            if self.destroyed:
                raise BotUserError("The player was closed. Try again.")
            if self.current is None:
                raise NothingPlaying()
            if len(self.votes) == 0:
                self.start_timer("vote_expiry", 60.0, "vote_expiry")
            self.votes.add(user_id)
            needed = max(1, (humans_count // 2) + 1)
            if len(self.votes) >= needed:
                self.cancel_timer("vote_expiry")
                self.votes.clear()
                self._end_handled = True
                await self._advance_locked(skipped=True)
                return True, len(self.votes), needed
            return False, len(self.votes), needed

    def set_sleep(self, minutes: int) -> None:
        if minutes <= 0:
            self.cancel_timer("sleep")
        else:
            self.start_timer("sleep", minutes * 60.0, "sleep")

    def set_autoplay(self, enabled: bool) -> None:
        self.autoplay = enabled
        self.autoplay_failures = 0

    async def similar(self, count: int = 5) -> list[QueueItem]:
        async with self.lock:
            if self.current is None:
                raise NothingPlaying()
            current_track = self.current
            search_query = current_track.artist or current_track.title
            outcome = await self.services.loader.load(self.guild_id, search_query)
            seen = {clean_track_title(h.title).lower() for h in self.queue.get_history(50)}
            seen.add(clean_track_title(current_track.title).lower())
            for item in self.queue:
                seen.add(clean_track_title(item.title).lower())

            candidates: list[QueueItem] = []
            bot_avatar = self.services.backend.bot_avatar_url() if hasattr(self.services.backend, "bot_avatar_url") else None
            bot_id = self.services.backend.bot_id() if hasattr(self.services.backend, "bot_id") else 0
            req_id = current_track.requester_id if current_track and current_track.requester_id else bot_id
            curr_avatar = current_track.requester_avatar_url if current_track else None
            req_avatar = curr_avatar or bot_avatar
            for t in outcome.tracks:
                t_title = clean_track_title(getattr(t, "title", "") or "")
                if t_title.lower() not in seen:
                    seen.add(t_title.lower())
                    try:
                        validate_track(t, self.cfg.max_track_seconds)
                        candidates.append(QueueItem.from_track(t, req_id, requester_avatar_url=req_avatar))
                    except TrackTooLong:
                        continue
                if len(candidates) >= count:
                    break

            if candidates:
                res = self.queue.add_many(candidates)
                return candidates[:res.added]
            return []

    def set_loop(self, mode: LoopMode) -> None:
        self.queue.loop = mode
        self.schedule_card_update()

    # ------------------------------------------------------------------ filters

    async def set_eq(self, name: str, bands: list[dict[str, Any]]) -> None:
        async with self.lock:
            eq = validate_and_build_eq(bands)
            audio = self._audio_or_raise()
            await self._audio_call(audio.set_filter(eq))
            self.current_eq = name

    async def reset_eq(self) -> None:
        async with self.lock:
            audio = self.audio
            if audio is not None:
                await self._audio_call(audio.remove_filter(Equalizer))
            self.current_eq = None

    async def apply_filter(self, name: str, config: dict[str, Any]) -> None:
        async with self.lock:
            filters = validate_and_build_filters(config)
            audio = self._audio_or_raise()
            for fltr in filters:
                await self._audio_call(audio.set_filter(fltr))
            self.applied_filters.add(name)

    async def remove_filter(self, name: str, config: dict[str, Any]) -> None:
        async with self.lock:
            filter_types = get_filter_types_for_preset(config)
            audio = self.audio
            if audio is not None:
                for f_type in filter_types:
                    await self._audio_call(audio.remove_filter(f_type))
            self.applied_filters.discard(name)

    async def reset_filters(self) -> None:
        async with self.lock:
            audio = self.audio
            if audio is not None:
                await self._audio_call(audio.clear_filters())
            self.applied_filters.clear()
            self.current_eq = None

    async def set_speed(self, factor: float) -> None:
        async with self.lock:
            audio = self._audio_or_raise()
            clamped = max(0.5, min(2.0, round(factor, 2)))
            if abs(clamped - 1.0) < 0.01:
                await self._audio_call(audio.remove_filter(Timescale))
                self.applied_filters.discard("speed")
                self.applied_filters.discard("smooth")
            else:
                ts = Timescale()
                ts.update(speed=clamped, pitch=1.0, rate=1.0)
                await self._audio_call(audio.set_filter(ts))
                self.applied_filters.add("speed")

    # ---------------------------------------------------------------- teardown

    def shutdown(self) -> None:
        """Cancel every timer and task, drop the queue, and release callbacks."""
        self.destroyed = True
        self.cancel_prefetch()
        self.cancel_all_timers()
        flusher = getattr(self.services, "flusher", None)
        if flusher is not None:
            flusher.cancel(self.guild_id)
        # Stop the now playing view so it is unregistered from discord.py
        if self.nowplaying_view is not None:
            try:
                self.nowplaying_view.stop()
            except Exception as exc:
                log.debug("guild=%s failed stopping nowplaying_view: %s", self.guild_id, exc)
            self.nowplaying_view = None
        self.nowplaying_channel_id = None
        self.nowplaying_message_id = None
        self._card_coalesce_task = None
        self.tasks.cancel_all()
        self._timers.clear()
        self.votes.clear()
        self.autoplay = False
        self.autoplay_failures = 0
        self.applied_filters.clear()
        self.current_eq = None
        self.queue.clear()
        self.current = None
        self._expire_callback = None
