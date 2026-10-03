"""Voice protocol that hands voice updates to Lavalink instead of sending audio itself.

Everything the rest of the code calls on a voice client is implemented here:
connect, disconnect, cleanup (inherited), is_connected, channel, guild,
channel_id. The registry and watchdog only use is_connected().
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

log = logging.getLogger(__name__)

READY_TIMEOUT = 15.0


class LavalinkVoiceClient(discord.VoiceProtocol):
    def __init__(self, client: discord.Client, channel: discord.abc.Connectable) -> None:
        super().__init__(client, channel)
        self._lavalink = client.lavalink  # type: ignore[attr-defined]
        self._channel_id: int | None = None
        self._destroyed = False
        self._ready = asyncio.Event()

    @property
    def guild(self) -> discord.Guild:
        return self.channel.guild  # type: ignore[union-attr]

    @property
    def channel_id(self) -> int | None:
        return self._channel_id

    def is_connected(self) -> bool:
        """True while Discord reports the bot in a voice channel and we have not torn down."""
        return (not self._destroyed) and self._channel_id is not None

    async def on_voice_server_update(self, data: dict[str, Any]) -> None:
        await self._lavalink.voice_update_handler({"t": "VOICE_SERVER_UPDATE", "d": data})

    async def on_voice_state_update(self, data: dict[str, Any]) -> None:
        raw_channel_id = data.get("channel_id")
        await self._lavalink.voice_update_handler({"t": "VOICE_STATE_UPDATE", "d": data})
        if not raw_channel_id:
            # Kicked, disconnected, or the channel was deleted. Lavalink has been
            # told. Drop out of discord.py's voice client table so a reconnect is
            # possible; the registry destroys the player from the voice listener.
            self._channel_id = None
            self._destroyed = True
            self._ready.clear()
            self.cleanup()
            return
        self._channel_id = int(raw_channel_id)
        channel = self.client.get_channel(self._channel_id)
        if channel is not None:
            self.channel = channel  # type: ignore[assignment]
        self._ready.set()

    async def connect(self, *, timeout: float, reconnect: bool, self_deaf: bool = False, self_mute: bool = False) -> None:
        self._ready.clear()
        await self.guild.change_voice_state(channel=self.channel, self_mute=self_mute, self_deaf=self_deaf)
        await asyncio.wait_for(self._ready.wait(), timeout=min(timeout, READY_TIMEOUT))

    async def disconnect(self, *, force: bool = False) -> None:
        already_gone = self._destroyed
        self._destroyed = True
        self._channel_id = None
        try:
            if not already_gone:
                await self.guild.change_voice_state(channel=None)
        finally:
            self.cleanup()
