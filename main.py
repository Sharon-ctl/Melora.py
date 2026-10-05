"""Entry point: builds the bot, wires supervision, and handles process-level errors."""
from __future__ import annotations

import asyncio
import logging
import math
import random
import signal
import sys
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from lavalink.client import Client as LavalinkClient

from config import Config, ConfigError, load_config
from core.alerts import Alerter
from core.backend import DiscordBackend
from core.contracts import PlayerServices
from core.lavalink_service import LavalinkService
from core.managed_player import ManagedPlayer
from core.queue import QueueItem
from core.registry import PlayerRegistry
from core.spotify import SpotifyService, parse_spotify_url
from core.stats import collect_stats, heartbeat_line
from core.storage import Storage
from logging_setup import setup_logging
from utils import messages
from utils.cache import CooldownTracker
from utils.components_v2 import BaseCardView, TextDisplay, create_card_container
from utils.errors import handle_app_command_error
from utils.ratelimit import RateLimiter, extract_qualified_command_name
from utils.supervisor import TaskSupervisor

log = logging.getLogger("musicbot")

EXTENSIONS = ("cogs.music", "cogs.voice_events", "cogs.admin", "cogs.library", "cogs.settings", "cogs.filters")
SHUTDOWN_STEP_TIMEOUT = 20.0
EXIT_OK = 0
EXIT_CRASH = 1
EXIT_CONFIG = 2


def configure_dns_resolver() -> None:
    """Ensure aiohttp DNS resolution is robust on Windows.

    When aiodns/c-ares cannot locate Windows adapter DNS servers in the registry,
    it defaults nameservers to ['127.0.0.1'], which causes all DNS queries to fail
    with ClientConnectorDNSError. When this occurs, fall back to native ThreadedResolver
    (WinSock getaddrinfo).
    """
    try:
        import aiodns
        import aiohttp.connector
        import aiohttp.resolver

        r = aiodns.DNSResolver()
        if r.nameservers == ["127.0.0.1"] or not r.nameservers:
            aiohttp.connector.DefaultResolver = aiohttp.resolver.ThreadedResolver
    except Exception:
        pass


configure_dns_resolver()


def _no_prefix(bot: commands.Bot, message: discord.Message) -> str:
    """Prefix commands are not used. Message events are not even received."""
    return "\x00"


class MusicBot(commands.AutoShardedBot):
    def __init__(self, cfg: Config, alerter: Alerter) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        if cfg.mention_reply_enabled:
            intents.guild_messages = True
        cache_flags = discord.MemberCacheFlags.none()
        cache_flags.voice = True
        super().__init__(
            command_prefix=_no_prefix,
            intents=intents,
            max_messages=None,
            chunk_guilds_at_startup=False,
            member_cache_flags=cache_flags,
            owner_id=cfg.owner_id,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
            activity=discord.Activity(type=discord.ActivityType.listening, name="/help"),
            enable_debug_events=False,
        )
        self.cfg = cfg
        self.alerter = alerter
        from utils.timing import install_http_rate_limit_filter

        install_http_rate_limit_filter()
        self.supervisor = TaskSupervisor(on_critical=self._on_critical_task)
        self.started_at = time.monotonic()
        self.storage = Storage()
        self.spotify = SpotifyService(enabled=cfg.spotify_enabled)
        self.rate_limiter = RateLimiter()
        self._mention_cooldown = CooldownTracker(self.cfg.mention_reply_cooldown)
        self.lavalink: LavalinkClient
        self.loader: LavalinkService
        self.backend: DiscordBackend
        self.registry: PlayerRegistry
        self._startup_done = False
        self._shutdown_started = False
        # Pre-cached for fast on_message hot path (set once in on_ready)
        self._bot_user_id: int = 0
        self._managed_role_ids: frozenset[int] = frozenset()

    # ------------------------------------------------------------------ startup

    async def setup_hook(self) -> None:
        self.storage.start()
        cfg = self.cfg
        assert self.user is not None
        self.lavalink = LavalinkClient(self.user.id, player=ManagedPlayer)
        for node in cfg.nodes:
            self.lavalink.add_node(
                host=node.host,
                port=node.port,
                password=node.password,
                region=node.region,
                name=node.name,
                ssl=node.ssl,
            )
        self.loader = LavalinkService(self.lavalink, cfg)
        self.backend = DiscordBackend(self, self.lavalink)
        self.registry = PlayerRegistry(PlayerServices(cfg, self.backend, self.loader, self.storage))
        self.tree.on_error = handle_app_command_error
        self.tree.interaction_check = self._tree_interaction_check

        self.supervisor.start("alerter", self.alerter.run)
        self.supervisor.start("ratelimit-cleanup", self.rate_limiter.cleanup_loop)
        for extension in EXTENSIONS:
            await self.load_extension(extension)

        if cfg.sync_on_start:
            try:
                synced = await asyncio.wait_for(self.tree.sync(), timeout=60)
                log.info("Synced %d commands at startup", len(synced))
            except Exception:
                log.exception("Startup command sync failed")
        self.supervisor.start("watchdog", self.registry.watchdog_loop)
        self.supervisor.start("heartbeat", self._heartbeat_loop)
        self.supervisor.start("daily-backup", self._daily_backup_loop)

    async def _tree_interaction_check(self, interaction: discord.Interaction) -> bool:
        """Central choke point for slash commands and autocomplete rate limiting."""
        # Record start timestamp for latency tracking
        interaction.extras["_start_time"] = time.monotonic()

        if interaction.user.id == self.cfg.owner_id:
            return True

        if interaction.type == discord.InteractionType.autocomplete:
            allowed, _ = self.rate_limiter.acquire_autocomplete(interaction.user.id, self.cfg.owner_id)
            if not allowed:
                try:
                    await interaction.response.send_autocomplete([])
                except Exception as exc:
                    log.debug("Autocomplete rate limit response ignored: %s", exc)
                return False
            return True

        if interaction.type == discord.InteractionType.application_command:
            cmd_name = extract_qualified_command_name(interaction)
            allowed, retry_after = self.rate_limiter.acquire_command(
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
                command_name=cmd_name,
                owner_id=self.cfg.owner_id,
            )
            if not allowed:
                sec = max(1, math.ceil(retry_after))
                if not interaction.response.is_done():
                    try:
                        await interaction.response.send_message(
                            messages.rate_limited(sec),
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.HTTPException as exc:
                        if exc.code not in (40060, 10062):
                            log.debug("Failed sending rate limit response: %s", exc)
                return False

        return True

    async def on_app_command_completion(
        self, interaction: discord.Interaction, command: app_commands.Command[Any, ..., Any] | app_commands.ContextMenu
    ) -> None:
        """Record execution time for completed commands."""
        extras = getattr(interaction, "extras", None)
        if not isinstance(extras, dict):
            return
        start_time = extras.get("_start_time")
        if isinstance(start_time, (int, float)):
            duration = time.monotonic() - start_time
            cmd_name = command.qualified_name if command else "unknown"
            from utils.timing import record_handler_time

            record_handler_time(cmd_name, duration)

    async def on_shard_ready(self, shard_id: int) -> None:
        log.info("Shard %d is ready (%d total shards)", shard_id, getattr(self, "shard_count", 1))

    async def on_shard_disconnect(self, shard_id: int) -> None:
        log.warning("Shard %d disconnected", shard_id)

    async def on_shard_resumed(self, shard_id: int) -> None:
        log.info("Shard %d resumed session", shard_id)

    def dispatch(self, event_name: str, /, *args: Any, **kwargs: Any) -> None:
        if event_name == "message" and not getattr(self.cfg, "mention_reply_enabled", True):
            return
        super().dispatch(event_name, *args, **kwargs)

    async def on_ready(self) -> None:
        guild_count = len(self.guilds)
        log.info("Ready as %s in %d guilds (%d shards)", self.user, guild_count, getattr(self, "shard_count", 1))
        if getattr(self.cfg, "mention_reply_enabled", True) and guild_count > 500:
            log.warning(
                "MENTION_REPLY_ENABLED is true with %d servers: every message event is delivered to the bot. "
                "For lower event-loop lag and gateway traffic, set MENTION_REPLY_ENABLED=false.",
                guild_count,
            )
        if not self._startup_done:
            self._startup_done = True
            # Cache IDs for zero-alloc on_message early-exit
            if self.user is not None:
                self._bot_user_id = self.user.id
            self.supervisor.start("startup-sweep", self._startup_sweep, restart=False)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type == discord.InteractionType.component:
            self.supervisor.spawn(
                self._guard_component_interaction(interaction),
                name=f"guard-comp-{interaction.id}",
            )

    async def _guard_component_interaction(self, interaction: discord.Interaction) -> None:
        """Catch-all for dead components. Waits ~1.5s; if unhandled, responds ephemerally."""
        await asyncio.sleep(1.5)
        if interaction.response.is_done():
            return

        # Check if the pressed message is a nowplaying card not matching current guild card
        data = getattr(interaction, "data", {})
        custom_id = str(data.get("custom_id", "")) if isinstance(data, dict) else ""
        if custom_id.startswith("np:") and interaction.guild_id and interaction.message:
            player = self.registry.get(interaction.guild_id)
            curr_card_id = getattr(player, "_last_card_message_id", None) if player else None
            if interaction.message.id != curr_card_id:
                try:
                    await interaction.message.delete()
                except Exception as exc:
                    log.debug("Failed deleting stale nowplaying card: %s", exc)

        if not interaction.response.is_done():
            try:
                await interaction.response.send_message(
                    messages.menu_expired(),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                if exc.code not in (40060, 10062):
                    log.debug("Component guard response failed: %s", exc)
            except Exception as exc:
                log.debug("Component guard response error: %s", exc)

    async def on_message(self, message: discord.Message) -> None:
        # Hot path: reject immediately if disabled or non-mentions with zero awaits / zero allocations
        if not getattr(self.cfg, "mention_reply_enabled", True):
            return
        if message.author.bot:
            return
        if message.guild is None:
            return
        if getattr(self, "is_load_shedding", False) or getattr(getattr(self, "registry", None), "is_load_shedding", False):
            return
        bot_uid = self._bot_user_id
        if not bot_uid:
            if self.user is not None:
                bot_uid = self._bot_user_id = self.user.id
            else:
                return

        # Fast mention check against cached id -- avoids iterating managed roles for 99%+ of messages
        raw_mentions = getattr(message, "raw_mentions", None)
        if raw_mentions is not None:
            user_mentioned = bot_uid in raw_mentions
        else:
            user_mentioned = any(getattr(m, "id", None) == bot_uid for m in getattr(message, "mentions", []))
        role_mentioned = False
        if not user_mentioned:
            raw_role_mentions = getattr(message, "raw_role_mentions", None)
            if raw_role_mentions:
                # Lazy-init managed role ids the first time we need them
                cached_roles = self._managed_role_ids
                if not cached_roles:
                    me = message.guild.me
                    if me is not None:
                        cached_roles = frozenset(
                            r.id for r in getattr(me, "roles", []) if hasattr(r, "is_bot_managed") and r.is_bot_managed()
                        )
                        self._managed_role_ids = cached_roles
                if cached_roles:
                    role_mentioned = not cached_roles.isdisjoint(raw_role_mentions)

        if not (user_mentioned or role_mentioned):
            return

        # --- Below here is the "slow path" that only runs on actual mentions ---
        try:
            # Ignore pure reply-pings when the user didn't explicitly include mention text
            if message.reference is not None:
                content = getattr(message, "content", "") or ""
                has_user_text = f"<@{bot_uid}>" in content or f"<@!{bot_uid}>" in content
                has_role_text = (
                    any(f"<@&{rid}>" in content for rid in self._managed_role_ids)
                    if self._managed_role_ids
                    else False
                )
                if not (has_user_text or has_role_text):
                    log.debug("on_message ignored: reply ping without explicit mention")
                    return

            channel = message.channel
            if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
                log.debug("on_message ignored: unsupported channel type %s", type(channel).__name__)
                return
            me = message.guild.me
            if me is None:
                log.debug("on_message ignored: bot member not found in guild")
                return
            perms = channel.permissions_for(me)
            send_allowed = perms.send_messages_in_threads if isinstance(channel, discord.Thread) else perms.send_messages
            if not (perms.view_channel and send_allowed):
                log.debug("on_message ignored: no send permission in channel %s", getattr(channel, "id", None))
                return

            # Respect per-user cooldown
            retry_after = self._mention_cooldown.trigger(message.author.id)
            if retry_after > 0:
                log.debug("on_message ignored: author %s on cooldown (%.1fs remaining)", message.author.id, retry_after)
                return

            text = TextDisplay(messages.mention_reply_card())
            container = create_card_container(text)
            view = BaseCardView(timeout=60.0)
            view.add_item(container)
            await message.reply(
                view=view,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            log.warning("Failed to send mention reply card: %s", exc)

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        log.exception("Unhandled exception in event %s", event_method)

    # -------------------------------------------------------- supervised tasks

    async def _startup_sweep(self) -> None:
        """Destroy players and voice presence left over from a previous process."""
        await asyncio.sleep(3.0)
        fixed = await self.registry.reconcile("startup sweep")
        log.info("Startup sweep finished (%d fixed)", fixed)

        # 24/7 rejoin and queue restore staggered with bounded concurrency and jitter
        guilds = list(self.guilds)
        total_guilds = len(guilds)
        rejoined_count = 0
        restored_tracks = 0
        restored_guilds = 0
        sem = asyncio.Semaphore(16)

        async def _process_guild(guild: discord.Guild) -> None:
            nonlocal rejoined_count, restored_tracks, restored_guilds
            if getattr(guild, "unavailable", False):
                return
            if hasattr(self, "get_shard"):
                shard = self.get_shard(guild.shard_id)
                if shard is not None and shard.is_closed():
                    log.debug("guild=%s shard %d is closed; skipping 24/7 rejoin", guild.id, guild.shard_id)
                    return
            async with sem:
                await asyncio.sleep(random.uniform(0.01, 0.05))
                try:
                    s = await self.storage.get_guild_settings(guild.id)
                    if s.voice_247_channel_id:
                        vc = guild.get_channel(s.voice_247_channel_id)
                        me = guild.me
                        if isinstance(vc, (discord.VoiceChannel, discord.StageChannel)) and isinstance(me, discord.Member):
                            perms = vc.permissions_for(me)
                            if perms.view_channel and perms.connect and perms.speak:
                                text_id = s.restrict_channel_id
                                if not text_id and guild.text_channels:
                                    text_id = guild.text_channels[0].id
                                player = await self.registry.get_or_create(guild.id, vc.id, text_id or 0)
                                player.is_247 = True
                                rejoined_count += 1
                                if s.restore_queue:
                                    snapshot = await self.storage.get_queue_snapshot(guild.id)
                                    if snapshot:
                                        has_avatar_fn = hasattr(self.backend, "bot_avatar_url")
                                        bot_avatar = self.backend.bot_avatar_url() if has_avatar_fn else None
                                        bot_id = self.backend.bot_id() if hasattr(self.backend, "bot_id") else 0
                                        chunk_size = 500
                                        total_for_guild = 0
                                        for i in range(0, len(snapshot), chunk_size):
                                            chunk = snapshot[i : i + chunk_size]
                                            items = [
                                                (
                                                    QueueItem.from_spotify(
                                                        t,
                                                        t.requester_id or bot_id,
                                                        requester_avatar_url=bot_avatar,
                                                    )
                                                    if parse_spotify_url(t.uri)
                                                    else QueueItem(
                                                        track=None,
                                                        title=t.title,
                                                        duration_ms=t.duration_ms,
                                                        requester_id=t.requester_id or bot_id,
                                                        artist=t.artist,
                                                        uri=t.uri,
                                                        query=t.uri,
                                                        requester_avatar_url=bot_avatar,
                                                    )
                                                )
                                                for t in chunk
                                            ]
                                            await player.enqueue(items)
                                            total_for_guild += len(items)
                                            if i + chunk_size < len(snapshot):
                                                await asyncio.sleep(0)
                                        await self.storage.delete_queue_snapshot(guild.id)
                                        restored_tracks += total_for_guild
                                        restored_guilds += 1
                                        if total_guilds <= 100:
                                            log.info("guild=%s restored %d tracks from snapshot", guild.id, total_for_guild)
                except Exception as exc:
                    log.debug("guild=%s 24/7 rejoin failed: %s", guild.id, exc)

        tasks = [_process_guild(g) for g in guilds]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if total_guilds > 100:
            log.info(
                "Startup 24/7 complete for %d servers: %d rejoined, %d tracks restored across %d servers",
                total_guilds,
                rejoined_count,
                restored_tracks,
                restored_guilds,
            )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.heartbeat_interval)
            try:
                log.info(heartbeat_line(collect_stats(self)))
            except Exception:
                log.exception("heartbeat failed; continuing")

    async def _daily_backup_loop(self) -> None:
        backup_dir = Path(self.cfg.db_path).resolve().parent / "backups"
        while True:
            try:
                if self.storage is not None:
                    dest = await self.storage.perform_daily_backup(backup_dir, keep=self.cfg.backup_count)
                    log.info("Database backup created: %s", dest.name)
            except Exception as exc:
                log.warning("Daily backup failed: %s", exc)
            await asyncio.sleep(86400.0)

    async def _on_critical_task(self, name: str, failures: int, exc: BaseException) -> None:
        self.alerter.submit(f"Background task {name} keeps failing ({failures} failures): {type(exc).__name__}")

    # ----------------------------------------------------------------- shutdown

    async def close(self) -> None:
        if not self._shutdown_started:
            self._shutdown_started = True
            await self._graceful_shutdown()
        await super().close()

    async def _graceful_shutdown(self) -> None:
        log.info("Shutting down")
        await self._step("stop background tasks", self.supervisor.stop_all())
        from utils.components_v2 import ACTIVE_VIEWS
        await self._step("close active views", ACTIVE_VIEWS.close_all())
        registry = getattr(self, "registry", None)
        if registry is not None:
            registry.stop_background_tasks()
            await self._step("destroy players", registry.destroy_all("shutdown"))
        lavalink_client = getattr(self, "lavalink", None)
        if lavalink_client is not None:
            await self._step("close lavalink client", lavalink_client.close())
        storage = getattr(self, "storage", None)
        if storage is not None:
            await self._step("close storage", storage.close())
        spotify = getattr(self, "spotify", None)
        if spotify is not None:
            await self._step("close spotify service", spotify.close())
        await self._step("close alerter", self.alerter.close())

    @staticmethod
    async def _step(label: str, work: Any) -> None:
        try:
            await asyncio.wait_for(work, timeout=SHUTDOWN_STEP_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Shutdown step failed: %s", label)


# ------------------------------------------------------------- process handlers


def install_process_handlers(loop: asyncio.AbstractEventLoop, alerter: Alerter) -> None:
    """Last-resort handlers so nothing fails silently."""

    def loop_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        message = context.get("message", "unhandled loop error")
        if isinstance(exc, BaseException):
            log.error("Event loop error: %s", message, exc_info=(type(exc), exc, exc.__traceback__))
            alerter.submit(f"Event loop error: {message} ({type(exc).__name__})")
        else:
            log.error("Event loop error: %s", message)

    def hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit or args.exc_value is None:
            return
        log.critical(
            "Uncaught exception in thread %s",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    loop.set_exception_handler(loop_handler)
    sys.excepthook = hook
    threading.excepthook = thread_hook


async def amain(cfg: Config) -> None:
    loop = asyncio.get_running_loop()
    alerter = Alerter(cfg)
    install_process_handlers(loop, alerter)
    bot = MusicBot(cfg, alerter)
    main_task = asyncio.current_task()
    if main_task is not None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, main_task.cancel)
            except (NotImplementedError, RuntimeError) as exc:
                log.debug("Signal handler registration skipped: %s", exc)
    async with bot:
        await bot.start(cfg.token, reconnect=True)


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    setup_logging(cfg)
    try:
        asyncio.run(amain(cfg))
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Stopped by user")
        return EXIT_OK
    except discord.LoginFailure:
        log.critical("Login failed: Discord rejected the bot token")
        return EXIT_CONFIG
    except Exception:
        log.critical("Fatal error", exc_info=True)
        return EXIT_CRASH
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
