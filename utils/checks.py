"""Reusable command checks. They raise BotUserError subclasses with clear messages."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from config import Config
from utils.errors import DJRequired, NotInGuild, NotInVoice, OwnerOnly

if TYPE_CHECKING:
    from main import MusicBot


def guild_member(interaction: discord.Interaction) -> discord.Member:
    """Return the invoking member or raise if the command ran outside a server."""
    user = interaction.user
    if interaction.guild is None or not isinstance(user, discord.Member):
        raise NotInGuild()
    return user


def user_voice_channel(interaction: discord.Interaction) -> discord.VoiceChannel | discord.StageChannel:
    """Return the voice channel the invoker is in, or raise."""
    member = guild_member(interaction)
    voice = member.voice
    if voice is None or voice.channel is None:
        raise NotInVoice()
    return voice.channel


def is_privileged(bot: MusicBot, member: discord.Member) -> bool:
    """Owner and administrators bypass the DJ role."""
    return member.id == bot.cfg.owner_id or member.guild_permissions.administrator


def require_dj(
    bot: MusicBot,
    cfg: Config,
    member: discord.Member,
    *,
    dj_role_id: int = 0,
    dj_only: bool = False,
) -> None:
    """Raise unless DJ mode is off, the member is privileged, or has the DJ role."""
    effective_role = dj_role_id or cfg.dj_role_id
    if effective_role == 0 and not dj_only:
        return
    if is_privileged(bot, member):
        return
    if effective_role != 0 and any(role.id == effective_role for role in member.roles):
        return
    raise DJRequired()


def owner_only() -> app_commands.Check:
    """Slash command check restricting a command to the configured owner."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id != interaction.client.cfg.owner_id:  # type: ignore[attr-defined]
            raise OwnerOnly()
        return True

    return app_commands.check(predicate)


def check_bot_channel_permissions(
    interaction: discord.Interaction,
    *,
    voice_channel: discord.VoiceChannel | discord.StageChannel | None = None,
) -> None:
    """Ensure bot has permissions for speaking, viewing channel, sending message, and embedding links/files."""
    guild = interaction.guild
    if guild is None:
        return
    me = guild.me
    if not isinstance(me, discord.Member):
        return

    # 1. Voice channel permissions: connect, speak, view_channel
    if voice_channel is not None:
        v_perms = voice_channel.permissions_for(me)
        missing_voice: list[str] = []
        if not v_perms.view_channel:
            missing_voice.append("view_channel")
        if not v_perms.connect:
            missing_voice.append("connect")
        if not v_perms.speak:
            missing_voice.append("speak")
        if missing_voice:
            raise app_commands.BotMissingPermissions(missing_voice)

    # 2. Text channel permissions: view_channel, send_messages, embed_links, attach_files
    channel = interaction.channel
    if channel is not None and hasattr(channel, "permissions_for"):
        t_perms = channel.permissions_for(me)  # type: ignore[union-attr]
        missing_text: list[str] = []
        if not t_perms.view_channel:
            missing_text.append("view_channel")
        if isinstance(channel, discord.Thread):
            if not t_perms.send_messages_in_threads:
                missing_text.append("send_messages_in_threads")
        elif not t_perms.send_messages:
            missing_text.append("send_messages")
        if not t_perms.embed_links:
            missing_text.append("embed_links")
        if not t_perms.attach_files:
            missing_text.append("attach_files")
        if missing_text:
            raise app_commands.BotMissingPermissions(missing_text)
