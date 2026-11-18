"""Shared base for cogs that control an existing guild player."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from core.guild_player import GuildPlayer
from utils import messages
from utils.checks import guild_member, require_dj
from utils.errors import BotUserError, NothingPlaying, NotInVoice, WrongChannel
from utils.text import clean, format_duration

if TYPE_CHECKING:
    from main import MusicBot

log = logging.getLogger(__name__)


class PlayerCog(commands.Cog):
    """Common checks and helpers for cogs that act on the running player."""

    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot

    async def cog_app_command_check(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if guild is None or self.bot is None:
            return True
        storage = getattr(self.bot, "storage", None)
        if storage is None:
            return True
        try:
            settings = await storage.get_guild_settings(guild.id)
            if settings.restrict_channel_id and interaction.channel_id != settings.restrict_channel_id:
                raise BotUserError(messages.channel_restricted(settings.restrict_channel_id))
        except BotUserError:
            raise
        except Exception as exc:
            log.debug("guild=%s failed checking channel restriction: %s", guild.id, exc)
        return True

    def _control_gate(self, interaction: discord.Interaction, *, dj: bool = False) -> GuildPlayer:
        """Checks shared by every command that controls an existing player."""
        member = guild_member(interaction)
        player = self.bot.registry.get(interaction.guild_id or 0)
        if player is None:
            raise NothingPlaying()
        voice = member.voice
        if voice is None or voice.channel is None:
            raise NotInVoice()
        if voice.channel.id != player.voice_channel_id:
            raise WrongChannel(channel_id=player.voice_channel_id)
        effective_dj = dj or getattr(player, "dj_only", False)
        if effective_dj:
            require_dj(
                self.bot,
                self.bot.cfg,
                member,
                dj_role_id=getattr(player, "dj_role_id", 0),
                dj_only=getattr(player, "dj_only", False),
            )
        return player

    async def _queue_pos_autocomplete(
        self, interaction: discord.Interaction, current: int | str
    ) -> list[app_commands.Choice[int]]:
        try:
            if not interaction.guild or not interaction.guild_id:
                return []
            player = self.bot.registry.get(interaction.guild_id)
            if player is None or len(player.queue) == 0:
                return []
            typed = str(current).strip().lower()
            if typed.isdigit():
                pos = int(typed)
                target_positions: list[int] = []
                queue_len = len(player.queue)
                if 1 <= pos <= queue_len:
                    target_positions.append(pos)
                base = pos * 10
                while len(target_positions) < 25 and base <= queue_len:
                    for p in range(base, min(base + 10, queue_len + 1)):
                        target_positions.append(p)
                        if len(target_positions) >= 25:
                            break
                    base *= 10

                choices: list[app_commands.Choice[int]] = []
                for p in target_positions:
                    item = player.queue[p - 1]
                    dur = format_duration(item.duration_ms) if item.duration_ms else "0:00"
                    if item.artist:
                        label = f"{p}. {item.title} - {item.artist} ({dur})"
                    else:
                        label = f"{p}. {item.title} ({dur})"
                    label = clean(label)[:100]
                    choices.append(app_commands.Choice(name=label, value=p))
                return choices

            matches: list[app_commands.Choice[int]] = []
            for idx, item in enumerate(player.queue, start=1):
                idx_str = str(idx)
                if typed:
                    num_match = idx_str.startswith(typed)
                    title_match = typed in (item.title or "").lower()
                    artist_match = bool(item.artist and typed in item.artist.lower())
                    if not (num_match or title_match or artist_match):
                        continue
                dur = format_duration(item.duration_ms) if item.duration_ms else "0:00"
                if item.artist:
                    label = f"{idx}. {item.title} - {item.artist} ({dur})"
                else:
                    label = f"{idx}. {item.title} ({dur})"
                label = clean(label)[:100]
                matches.append(app_commands.Choice(name=label, value=idx))
                if len(matches) >= 25:
                    break
            return matches
        except Exception:
            return []
