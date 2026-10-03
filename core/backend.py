"""The real PlayerBackend: discord.py voice plus the lavalink.py client.

Every outbound call has a timeout and a try/except. Failures are logged and
converted into the project's error types; they never propagate raw.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import discord
from lavalink.client import Client
from lavalink.errors import ClientError

from core.contracts import AudioPlayer
from core.voice_client import LavalinkVoiceClient
from utils.errors import NodeOffline, VoiceConnectFailed

if TYPE_CHECKING:
    from main import MusicBot

log = logging.getLogger(__name__)

CALL_TIMEOUT = 10.0
CONNECT_TIMEOUT = 15.0
_NO_MENTIONS = discord.AllowedMentions.none()


class DiscordBackend:
    def __init__(self, bot: MusicBot, lavalink_client: Client) -> None:
        self._bot = bot
        self._ll = lavalink_client

    # ------------------------------------------------------------------ queries

    def audio(self, guild_id: int) -> AudioPlayer | None:
        return self._ll.player_manager.get(guild_id)  # type: ignore[return-value]

    def guild_ids(self) -> set[int]:
        return {guild.id for guild in self._bot.guilds}

    def any_node_available(self) -> bool:
        return bool(self._ll.node_manager.available_nodes)

    def bot_display_name(self, guild_id: int) -> str:
        guild = self._bot.get_guild(guild_id)
        if guild is not None and guild.me is not None:
            return guild.me.display_name
        if self._bot.user is not None:
            return self._bot.user.name
        return "Melora"

    def bot_avatar_url(self) -> str | None:
        user = self._bot.user
        if user is None:
            return None
        try:
            return str(user.display_avatar.with_size(128).with_format("png").url)
        except Exception:
            return str(getattr(user.display_avatar, "url", "")) or None

    def bot_id(self) -> int:
        return self._bot.user.id if self._bot.user else 0

    @staticmethod
    def _bot_voice_channel_id(guild: discord.Guild) -> int | None:
        voice = getattr(guild.me, "voice", None)
        channel = getattr(voice, "channel", None)
        return channel.id if channel is not None else None

    def voice_connected(self, guild_id: int, channel_id: int | None) -> bool:
        """True only if both our voice client and Discord agree the bot is in the channel."""
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            return False
        voice_client = guild.voice_client
        if voice_client is None:
            return False
        try:
            if not voice_client.is_connected():
                return False
        except Exception as exc:
            log.debug("guild=%s voice_client.is_connected error: %s", guild_id, exc)
            return False
        actual = self._bot_voice_channel_id(guild)
        if actual is None:
            return False
        return channel_id is None or actual == channel_id

    def humans_in_channel(self, guild_id: int, channel_id: int) -> int | None:
        guild = self._bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild is not None else None
        if guild is None or channel is None:
            return None
        bot_id = self._bot.user.id if self._bot.user else 0
        count = 0
        for user_id in channel.voice_states:  # type: ignore[union-attr]
            if user_id == bot_id:
                continue
            user = guild.get_member(user_id) or self._bot.get_user(user_id)
            if user is not None and getattr(user, "bot", False):
                continue
            count += 1
        return count

    def stray_guild_ids(self) -> set[int]:
        """Guilds with a Lavalink player or a bot voice presence, for leftover detection."""
        strays = set(self._ll.player_manager.players.keys())
        for guild in self._bot.guilds:
            if guild.voice_client is not None or self._bot_voice_channel_id(guild) is not None:
                strays.add(guild.id)
        return strays

    # ------------------------------------------------------------------ actions

    async def connect(self, guild_id: int, channel_id: int) -> None:
        guild = self._bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild is not None else None
        if channel is None or not hasattr(channel, "connect"):
            raise VoiceConnectFailed()
        try:
            self._ll.player_manager.create(guild_id)
        except ClientError:
            raise NodeOffline() from None
        try:
            await channel.connect(  # type: ignore[union-attr]
                cls=LavalinkVoiceClient, timeout=CONNECT_TIMEOUT, reconnect=False, self_deaf=True
            )
        except asyncio.CancelledError:
            await self.purge(guild_id)
            raise
        except Exception as exc:
            log.warning("guild=%s voice connect failed: %s", guild_id, type(exc).__name__)
            await self.purge(guild_id)
            raise VoiceConnectFailed() from None

    async def purge(self, guild_id: int) -> None:
        """Destroy the Lavalink player and disconnect voice. Idempotent, never raises."""
        await self._destroy_audio(guild_id)
        await self._disconnect_voice(guild_id)

    async def _destroy_audio(self, guild_id: int) -> None:
        manager = self._ll.player_manager
        player = manager.get(guild_id)
        if player is None:
            return
        try:
            if player.node.available:
                await asyncio.wait_for(manager.destroy(guild_id), timeout=CALL_TIMEOUT)
            else:
                manager.remove(guild_id)
        except asyncio.CancelledError:
            manager.remove(guild_id)
            raise
        except Exception as exc:
            log.warning("guild=%s lavalink player destroy failed: %s", guild_id, type(exc).__name__)
        finally:
            manager.remove(guild_id)

    async def _disconnect_voice(self, guild_id: int) -> None:
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            return
        voice_client = guild.voice_client
        try:
            if voice_client is not None:
                try:
                    await asyncio.wait_for(voice_client.disconnect(force=True), timeout=CALL_TIMEOUT)
                finally:
                    voice_client.cleanup()
            if self._bot_voice_channel_id(guild) is not None:
                await asyncio.wait_for(guild.change_voice_state(channel=None), timeout=CALL_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("guild=%s voice disconnect failed: %s", guild_id, type(exc).__name__)

    async def notify(self, channel_id: int, text: str) -> None:
        """Post a short notice. Skips quietly without Send Messages permission."""
        channel = self._bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            return
        guild = getattr(channel, "guild", None)
        me = guild.me if guild is not None else None
        if isinstance(me, discord.Member):
            perms = channel.permissions_for(me)  # type: ignore[union-attr]
            allowed = perms.send_messages_in_threads if isinstance(channel, discord.Thread) else perms.send_messages
            if not (perms.view_channel and allowed):
                return
        try:
            await asyncio.wait_for(
                channel.send(text[:1900], allowed_mentions=_NO_MENTIONS),  # type: ignore[union-attr]
                timeout=CALL_TIMEOUT,
            )
        except (discord.Forbidden, discord.NotFound) as exc:
            log.debug("notice delivery skipped (%s)", type(exc).__name__)
            return
        except (discord.HTTPException, asyncio.TimeoutError):
            log.debug("notice delivery failed", exc_info=True)

    async def send_nowplaying_card(self, channel_id: int, container: Any, view: Any) -> int | None:
        """Send the initial now playing card in the specified channel."""
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except Exception:
                return None
        if not hasattr(channel, "send"):
            return None
        guild = getattr(channel, "guild", None)
        me = guild.me if guild is not None else None
        if isinstance(me, discord.Member):
            perms = channel.permissions_for(me)  # type: ignore[union-attr]
            allowed = perms.send_messages_in_threads if isinstance(channel, discord.Thread) else perms.send_messages
            if not (perms.view_channel and allowed):
                log.info("Missing permission to send now playing card in channel %s", channel_id)
                return None
        try:
            view.clear_items()
            view.add_item(container)
            msg = await asyncio.wait_for(
                channel.send(view=view, allowed_mentions=_NO_MENTIONS),  # type: ignore[union-attr]
                timeout=CALL_TIMEOUT,
            )
            return msg.id
        except (discord.Forbidden, discord.NotFound) as exc:
            log.info("Now playing card send failed (%s)", type(exc).__name__)
            return None
        except Exception as exc:
            log.warning("Now playing card send error: %s", exc)
            return None

    async def edit_nowplaying_card(self, channel_id: int, message_id: int, container: Any, view: Any) -> bool:
        """Edit the existing now playing card message."""
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except Exception:
                return False
        if not hasattr(channel, "get_partial_message"):
            return False
        guild = getattr(channel, "guild", None)
        me = guild.me if guild is not None else None
        if isinstance(me, discord.Member):
            perms = channel.permissions_for(me)  # type: ignore[union-attr]
            if not perms.view_channel:
                return False
        try:
            msg = channel.get_partial_message(message_id)  # type: ignore[union-attr]
            view.clear_items()
            view.add_item(container)
            await asyncio.wait_for(
                msg.edit(view=view, allowed_mentions=_NO_MENTIONS),
                timeout=CALL_TIMEOUT,
            )
            return True
        except (discord.NotFound, discord.Forbidden):
            raise
        except Exception as exc:
            log.warning("Now playing card edit error: %s", exc)
            return False

    async def delete_nowplaying_card(self, channel_id: int, message_id: int) -> None:
        """Delete the now playing card message. Idempotent, never raises."""
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except Exception:
                return
        if not hasattr(channel, "get_partial_message"):
            return
        try:
            msg = channel.get_partial_message(message_id)  # type: ignore[union-attr]
            await asyncio.wait_for(msg.delete(), timeout=CALL_TIMEOUT)
        except (discord.NotFound, discord.Forbidden):
            log.debug("Card already deleted or lack permission to delete")
        except Exception as exc:
            log.debug("Now playing card delete skipped: %s", exc)

