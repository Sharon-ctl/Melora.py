"""Voice channel status manager and background flusher.

Manages dynamic voice channel status updates based on player lifecycle:
- Playing: <:music:1526006747603406869> {song title}
- Paused: Paused
- Idle: <:addmusic:1526007757826691232> Use /play to listen
- Cleared: None

Features:
- Single state machine function deriving desired status from player state.
- Supervised flusher task applying latest-wins updates with deduplication and rate pacing.
- Permission checking (set_voice_channel_status, connect) with 10-minute skip cache.
- Comprehensive error handling (403/404 skip, 400 emoji fallback remembered per guild, 429 backoff).
- Ordered clearing before disconnect on intentional leaves, best-effort on forced disconnects.
- Graceful shutdown clearing with 5s timeout.
- Bounded memory cleaned up by player destroy path.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord

from config import Config
from utils import messages

log = logging.getLogger(__name__)

DEFAULT_MIN_INTERVAL = 3.0
LOAD_SHEDDING_INTERVAL = 6.0
PERMISSION_SKIP_SECONDS = 600.0  # 10 minutes


def derive_desired_status(
    player: Any | None,
    *,
    enabled: bool = True,
    use_emoji: bool = True,
) -> str | None:
    """Derive desired voice channel status from player state.

    States:
    - disabled or no player or player destroyed: None (cleared)
    - playing: <:music:id> {sanitized_title} (or {sanitized_title} if no emoji)
    - paused: Paused
    - idle: <:addmusic:id> Use /play to listen (or Use /play to listen if no emoji)
    """
    if not enabled or player is None or getattr(player, "destroyed", False):
        return None

    current = getattr(player, "current", None)
    if current is not None:
        if getattr(player, "paused", False):
            return messages.voice_status_paused()
        title = getattr(current, "title", "") or ""
        return messages.voice_status_playing(title, use_emoji=use_emoji)

    # Player exists and connected, but no current track -> idle
    return messages.voice_status_idle(use_emoji=use_emoji)


class VoiceStatusManager:
    """Central manager for voice channel statuses across guilds."""

    def __init__(self, bot: Any, cfg: Config, storage: Any | None = None) -> None:
        self._bot = bot
        self._cfg = cfg
        self._storage = storage

        # State tracking per guild
        self._desired: dict[int, str | None] = {}
        self._desired_channel: dict[int, int] = {}
        self._current_applied: dict[int, str | None] = {}
        self._last_applied_at: dict[int, float] = {}
        self._backoff_until: dict[int, float] = {}
        self._emoji_disabled_guilds: set[int] = set()
        self._missing_perm_guilds: set[int] = set()
        self._skipped_channels: dict[int, float] = {}

        self._load_shedding = False
        self._stopped = False
        self._task: asyncio.Task[None] | None = None

    @property
    def missing_permission_count(self) -> int:
        """Count of guilds currently lacking set_voice_channel_status permission."""
        return len(self._missing_perm_guilds)

    def set_load_shedding(self, active: bool) -> None:
        """Adjust min update interval when load shedding is active."""
        self._load_shedding = active

    def start(self) -> None:
        """Start the background flusher task."""
        self._stopped = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="voice-status-flusher")

    def stop(self) -> None:
        """Stop the background flusher task."""
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ---------------------------------------------------------------- lifecycle hooks

    def request_update(self, guild_id: int, player: Any | None = None) -> None:
        """Derive desired status and schedule flusher update (latest-wins, non-blocking)."""
        if self._stopped or not getattr(self._cfg, "voice_status_enabled", True):
            return

        if player is None and hasattr(self._bot, "registry"):
            player = self._bot.registry.get(guild_id)

        if player is None or getattr(player, "destroyed", False):
            # No player -> clear
            channel_id = self._desired_channel.get(guild_id, 0)
            self._set_desired(guild_id, channel_id, None)
            return

        channel_id = getattr(player, "voice_channel_id", 0) or self._desired_channel.get(guild_id, 0)

        # Check guild settings if cached
        enabled = True
        if self._storage is not None and hasattr(self._storage, "_settings_cache"):
            s = self._storage._settings_cache.get(guild_id)
            if s is not None:
                enabled = getattr(s, "voice_status_enabled", True)

        use_emoji = (
            getattr(self._cfg, "voice_status_use_emoji", True)
            and guild_id not in self._emoji_disabled_guilds
        )

        desired = derive_desired_status(player, enabled=enabled, use_emoji=use_emoji)
        self._set_desired(guild_id, channel_id, desired)

    def _set_desired(self, guild_id: int, channel_id: int, desired: str | None) -> None:
        self._desired[guild_id] = desired
        if channel_id:
            self._desired_channel[guild_id] = channel_id

    def on_track_start(self, guild_id: int, player: Any | None = None) -> None:
        self.request_update(guild_id, player)

    def on_pause(self, guild_id: int, player: Any | None = None) -> None:
        self.request_update(guild_id, player)

    def on_resume(self, guild_id: int, player: Any | None = None) -> None:
        self.request_update(guild_id, player)

    def on_queue_ended(self, guild_id: int, player: Any | None = None) -> None:
        self.request_update(guild_id, player)

    async def on_channel_moved(self, guild_id: int, old_channel_id: int, new_channel_id: int) -> None:
        """Bot moved channels: clear status from old channel and set on new channel."""
        # 1. Clear status from old channel (best effort)
        try:
            await self._apply_channel_status(guild_id, old_channel_id, None)
        except Exception as exc:
            log.debug("guild=%s failed clearing moved channel status: %s", guild_id, exc)

        # 2. Reset applied state so dedupe does not suppress new channel update
        self._current_applied[guild_id] = None
        self._desired_channel[guild_id] = new_channel_id

        # 3. Schedule update for new channel
        self.request_update(guild_id)

    async def on_setting_disabled(self, guild_id: int) -> None:
        """Guild disabled voice status setting: clear immediately."""
        channel_id = self._desired_channel.get(guild_id, 0)
        self._desired.pop(guild_id, None)
        try:
            await self._apply_channel_status(guild_id, channel_id, None)
        except Exception as exc:
            log.debug("guild=%s failed clearing disabled setting status: %s", guild_id, exc)

    async def on_setting_enabled(self, guild_id: int) -> None:
        """Guild enabled voice status setting: request update."""
        self.request_update(guild_id)

    async def on_player_destroy(self, guild_id: int, player: Any | None = None, *, forced: bool = False) -> None:
        """Player destroyed: clear status and release all per-guild state."""
        # Cancel any pending flusher update
        self._desired.pop(guild_id, None)

        channel_id = 0
        if player is not None:
            channel_id = getattr(player, "voice_channel_id", 0)
        if not channel_id:
            channel_id = self._desired_channel.get(guild_id, 0)

        # Clear status if one was applied
        if channel_id and self._current_applied.get(guild_id) is not None:
            if not forced:
                # Intentional leave: clear status BEFORE disconnecting voice, awaited with 2s timeout
                try:
                    await asyncio.wait_for(
                        self._apply_channel_status(guild_id, channel_id, None),
                        timeout=2.0,
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    log.debug("guild=%s timeout clearing voice status before leave", guild_id)
                except Exception as exc:
                    log.debug("guild=%s error clearing voice status before leave: %s", guild_id, exc)
            else:
                # Forced disconnect (kick, 4014, channel deleted): best effort
                try:
                    await asyncio.wait_for(
                        self._apply_channel_status(guild_id, channel_id, None),
                        timeout=1.0,
                    )
                except (discord.Forbidden, discord.NotFound, asyncio.TimeoutError, TimeoutError) as exc:
                    log.debug("guild=%s forced disconnect status clear ignored: %s", guild_id, type(exc).__name__)
                except Exception as exc:
                    log.debug("guild=%s forced disconnect status clear error: %s", guild_id, exc)

        # Bound memory: drop all per-guild tracking state
        self.remove_guild(guild_id, channel_id)

    def remove_guild(self, guild_id: int, channel_id: int = 0) -> None:
        """Purge all in-memory tracking state for a guild."""
        self._desired.pop(guild_id, None)
        ch = self._desired_channel.pop(guild_id, channel_id)
        self._current_applied.pop(guild_id, None)
        self._last_applied_at.pop(guild_id, None)
        self._backoff_until.pop(guild_id, None)
        self._emoji_disabled_guilds.discard(guild_id)
        self._missing_perm_guilds.discard(guild_id)
        if ch:
            self._skipped_channels.pop(ch, None)

    async def clear_all_shutdown(self, timeout: float = 5.0) -> None:
        """Clear all active voice statuses on graceful shutdown within overall timeout."""
        self.stop()
        tasks = []
        for guild_id, status in list(self._current_applied.items()):
            if status is not None:
                ch_id = self._desired_channel.get(guild_id, 0)
                if ch_id:
                    tasks.append(self._apply_channel_status(guild_id, ch_id, None))

        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=timeout,
                )
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("Timeout clearing voice statuses during shutdown")
            except Exception as exc:
                log.debug("Shutdown status clearing error: %s", exc)

    # ---------------------------------------------------------------- flusher loop

    async def run(self) -> None:
        """Background flusher applying latest-wins desired states per guild."""
        while not self._stopped:
            try:
                await asyncio.sleep(0.5)
                if not self._desired:
                    continue

                now = time.monotonic()
                min_interval = LOAD_SHEDDING_INTERVAL if self._load_shedding else DEFAULT_MIN_INTERVAL

                # Copy keys so dictionary iteration is stable
                guild_ids = list(self._desired)
                for guild_id in guild_ids:
                    if guild_id not in self._desired:
                        continue

                    # Check 429 backoff
                    if now < self._backoff_until.get(guild_id, 0.0):
                        continue

                    # Check minimum interval
                    last_applied = self._last_applied_at.get(guild_id, 0.0)
                    if now - last_applied < min_interval:
                        continue

                    desired = self._desired[guild_id]
                    applied = self._current_applied.get(guild_id)

                    # Deduplication: never call API if text is unchanged
                    if desired == applied:
                        self._desired.pop(guild_id, None)
                        continue

                    channel_id = self._desired_channel.get(guild_id, 0)
                    if not channel_id:
                        self._desired.pop(guild_id, None)
                        continue

                    # Check permission skip
                    if now < self._skipped_channels.get(channel_id, 0.0):
                        continue

                    # Apply status
                    try:
                        await self._apply_channel_status(guild_id, channel_id, desired)
                        self._current_applied[guild_id] = desired
                        self._last_applied_at[guild_id] = time.monotonic()
                        # If current desired didn't change while we awaited API, pop it
                        if self._desired.get(guild_id) == desired:
                            self._desired.pop(guild_id, None)
                    except Exception as exc:
                        log.debug("guild=%s flusher apply failed: %s", guild_id, exc)

                    # Yield between edits to keep event loop responsive
                    await asyncio.sleep(0)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.exception("VoiceStatusManager flusher loop error: %s", exc)
                await asyncio.sleep(1.0)

    # ---------------------------------------------------------------- api execution

    async def _apply_channel_status(self, guild_id: int, channel_id: int, status: str | None) -> None:
        """Execute channel status update with permission checking and error handling."""
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            return

        channel = guild.get_channel(channel_id)
        if channel is None:
            return

        # Permission pre-check: Set Voice Channel Status and Connect
        me = getattr(guild, "me", None)
        if me is not None and hasattr(channel, "permissions_for"):
            perms = channel.permissions_for(me)
            has_voice_status_perm = getattr(perms, "set_voice_channel_status", False)
            has_connect_perm = getattr(perms, "connect", False)

            if not (has_voice_status_perm and has_connect_perm):
                now = time.monotonic()
                if now >= self._skipped_channels.get(channel_id, 0.0):
                    log.debug(
                        "guild=%s channel=%s missing Set Voice Channel Status or Connect permission; skipping for 10m",
                        guild_id,
                        channel_id,
                    )
                    self._skipped_channels[channel_id] = now + PERMISSION_SKIP_SECONDS
                self._missing_perm_guilds.add(guild_id)
                return
            else:
                self._missing_perm_guilds.discard(guild_id)
                self._skipped_channels.pop(channel_id, None)

        if not hasattr(channel, "edit"):
            return

        try:
            await channel.edit(status=status)
            self._current_applied[guild_id] = status
            self._last_applied_at[guild_id] = time.monotonic()
        except (discord.Forbidden, discord.NotFound) as exc:
            # 403 or 404: skip quietly
            log.debug("guild=%s channel=%s status edit skipped quietly (%s)", guild_id, channel_id, type(exc).__name__)
        except discord.RateLimited as rl_exc:
            # 429: back off without blocking
            retry_after = getattr(rl_exc, "retry_after", 5.0) or 5.0
            self._backoff_until[guild_id] = time.monotonic() + float(retry_after)
            log.debug("guild=%s voice status 429 rate limit; backing off for %.1fs", guild_id, retry_after)
        except discord.HTTPException as http_exc:
            status_code = getattr(http_exc, "status", None)
            if status_code in (403, 404):
                log.debug("guild=%s channel=%s status edit HTTP %s skipped quietly", guild_id, channel_id, status_code)
            elif status_code == 429:
                retry_after = 5.0
                resp = getattr(http_exc, "response", None)
                if resp is not None and hasattr(resp, "headers"):
                    try:
                        retry_after = float(resp.headers.get("Retry-After", 5.0))
                    except (ValueError, TypeError) as conv_exc:
                        log.debug("Retry-after parse error: %s", conv_exc)
                self._backoff_until[guild_id] = time.monotonic() + float(retry_after)
                log.debug("guild=%s voice status 429 backoff %.1fs", guild_id, retry_after)
            elif status_code == 400 and status and ("<:" in status or "<a:" in status):
                # 400 on emoji markup: fall back to text-only version and remember it
                log.info("guild=%s custom emoji failed in voice status; falling back to text-only", guild_id)
                self._emoji_disabled_guilds.add(guild_id)
                player = self._bot.registry.get(guild_id) if hasattr(self._bot, "registry") else None
                fallback_status = derive_desired_status(player, enabled=True, use_emoji=False)
                try:
                    await channel.edit(status=fallback_status)
                    self._current_applied[guild_id] = fallback_status
                    self._last_applied_at[guild_id] = time.monotonic()
                except Exception as fb_exc:
                    log.debug("guild=%s text fallback voice status edit failed: %s", guild_id, fb_exc)
            else:
                log.warning("guild=%s voice status HTTP error: %s", guild_id, http_exc)
        except Exception as exc:
            log.warning("guild=%s unexpected voice status error: %s", guild_id, exc)
