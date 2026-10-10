"""Discord voice and guild events, plus Lavalink event hooks.

Every listener is wrapped with ``guarded`` so an exception is logged and
swallowed. Lavalink track events run inside a per-guild error boundary so a
failure in one guild only resets that guild's player.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from lavalink.events import (
    NodeChangedEvent,
    NodeConnectedEvent,
    NodeDisconnectedEvent,
    NodeReadyEvent,
    PlayerErrorEvent,
    TrackEndEvent,
    TrackExceptionEvent,
    TrackStartEvent,
    TrackStuckEvent,
    WebSocketClosedEvent,
)

from core.guild_player import GuildPlayer
from utils.errors import guarded
from utils.text import truncate

if TYPE_CHECKING:
    from main import MusicBot

log = logging.getLogger(__name__)

STALE_LEAVE_SECONDS = 2.0
NODE_SWEEP_DELAY = 3.0


class VoiceEvents(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        client = self.bot.lavalink
        client.add_event_hook(self.on_track_start, event=TrackStartEvent)
        client.add_event_hook(self.on_track_end, event=TrackEndEvent)
        client.add_event_hook(self.on_track_exception, event=TrackExceptionEvent)
        client.add_event_hook(self.on_track_stuck, event=TrackStuckEvent)
        client.add_event_hook(self.on_socket_closed, event=WebSocketClosedEvent)
        client.add_event_hook(self.on_player_error, event=PlayerErrorEvent)
        client.add_event_hook(self.on_node_connected, event=NodeConnectedEvent)
        client.add_event_hook(self.on_node_ready, event=NodeReadyEvent)
        client.add_event_hook(self.on_node_disconnected, event=NodeDisconnectedEvent)
        client.add_event_hook(self.on_node_changed, event=NodeChangedEvent)

    async def cog_unload(self) -> None:
        client = getattr(self.bot, "lavalink", None)
        if client is not None:
            client.clear_event_hooks()

    # --------------------------------------------------------- lavalink: tracks

    def _player_for(self, guild_id: int) -> GuildPlayer | None:
        return self.bot.registry.get(guild_id)

    @guarded
    async def on_track_start(self, event: TrackStartEvent) -> None:
        guild_id = event.player.guild_id
        player = self._player_for(guild_id)
        if player is not None:
            await self.bot.registry.run_guarded(guild_id, player.on_track_start())

    @guarded
    async def on_track_end(self, event: TrackEndEvent) -> None:
        guild_id = event.player.guild_id
        player = self._player_for(guild_id)
        if player is None:
            return
        encoded = event.track.track if event.track is not None else None
        await self.bot.registry.run_guarded(guild_id, player.on_track_end(encoded, str(event.reason.value)))

    @guarded
    async def on_track_exception(self, event: TrackExceptionEvent) -> None:
        guild_id = event.player.guild_id
        player = self._player_for(guild_id)
        if player is None:
            return
        message = event.message or event.cause
        await self.bot.registry.run_guarded(guild_id, player.on_track_exception(event.track.track, message))

    @guarded
    async def on_track_stuck(self, event: TrackStuckEvent) -> None:
        guild_id = event.player.guild_id
        player = self._player_for(guild_id)
        if player is not None:
            await self.bot.registry.run_guarded(guild_id, player.on_track_stuck(event.track.track))

    @guarded
    async def on_socket_closed(self, event: WebSocketClosedEvent) -> None:
        log.warning(
            "guild=%s voice websocket closed code=%s by_remote=%s reason=%s",
            event.player.guild_id,
            event.code,
            event.by_remote,
            truncate(event.reason, 80),
        )

    @guarded
    async def on_player_error(self, event: PlayerErrorEvent) -> None:
        log.warning("guild=%s player error: %s", event.player.guild_id, type(event.original).__name__)

    # ----------------------------------------------------------- lavalink: nodes

    @guarded
    async def on_node_connected(self, event: NodeConnectedEvent) -> None:
        log.info("Lavalink node %s connected", event.node.name)

    @guarded
    async def on_node_ready(self, event: NodeReadyEvent) -> None:
        log.info("Lavalink node %s ready (session resumed=%s)", event.node.name, event.resumed)
        self.bot.supervisor.start("node-sweep", self._node_sweep, restart=False)

    @guarded
    async def on_node_disconnected(self, event: NodeDisconnectedEvent) -> None:
        log.warning("Lavalink node %s disconnected (code=%s)", event.node.name, event.code)
        await self.bot.registry.handle_node_disconnected(event.node)
        if not self.bot.backend.any_node_available():
            self.bot.supervisor.start("node-loss-check", self._node_loss_check, restart=False)

    @guarded
    async def on_node_changed(self, event: NodeChangedEvent) -> None:
        log.info(
            "guild=%s moved from node %s to node %s",
            event.player.guild_id,
            event.old_node.name,
            event.new_node.name,
        )

    async def _node_sweep(self) -> None:
        """Runs once per reconnect (the supervisor refuses duplicates while one is running)."""
        await asyncio.sleep(NODE_SWEEP_DELAY)
        await self.bot.registry.reconcile("node reconnect")

    async def _node_loss_check(self) -> None:
        """If no node is back after the grace period, end all players and tell the channels."""
        await asyncio.sleep(self.bot.cfg.node_loss_grace)
        await self.bot.registry.handle_all_nodes_down()

    # ------------------------------------------------------------ discord events

    @commands.Cog.listener()
    @guarded
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        bot_user = self.bot.user
        if bot_user is None:
            return
        guild_id = member.guild.id
        player = self.bot.registry.get(guild_id)
        if player is None:
            return

        if member.id == bot_user.id:
            if after.channel is not None and after.channel.id == player.voice_channel_id:
                return
            if after.channel is None and player.age() < STALE_LEAVE_SECONDS:
                return  # stale event from a previous connection in the same guild
            if after.channel is not None:
                old_channel_id = player.voice_channel_id
                player.voice_channel_id = after.channel.id
                log.info("guild=%s bot moved voice channel: %s -> %s", guild_id, old_channel_id, after.channel.id)
                voice_status = getattr(self.bot, "voice_status", None)
                if voice_status is not None:
                    await voice_status.on_channel_moved(guild_id, old_channel_id, after.channel.id)
                return
            reason = "disconnected"
            log.info("guild=%s bot voice state changed: %s", guild_id, reason)
            await self.bot.registry.destroy(guild_id, reason)
            return

        before_id = before.channel.id if before.channel else None
        after_id = after.channel.id if after.channel else None
        if before_id == after_id or player.voice_channel_id not in (before_id, after_id):
            return
        humans = self.bot.backend.humans_in_channel(guild_id, player.voice_channel_id)
        if humans is None:
            await self.bot.registry.destroy(guild_id, "voice channel missing")
        elif humans == 0:
            if not player.has_timer("alone"):
                player.start_timer("alone", self.bot.cfg.alone_timeout, "alone")
        else:
            player.cancel_timer("alone")

    @commands.Cog.listener()
    @guarded
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        player = self.bot.registry.get(channel.guild.id)
        if player is not None and player.voice_channel_id == channel.id:
            await self.bot.registry.destroy(channel.guild.id, "voice channel deleted")

    @commands.Cog.listener()
    @guarded
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        if getattr(guild, "unavailable", False):
            return
        await self.bot.registry.on_guild_removed(guild.id)


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(VoiceEvents(bot))
