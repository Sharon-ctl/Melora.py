"""Server settings, default volume, and 24/7 mode commands."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from core.storage import StorageError
from utils import messages
from utils.checks import user_voice_channel
from utils.errors import BotUserError, MissingVoicePermissions, StageUnsupported
from utils.interaction import reply, safe_defer

if TYPE_CHECKING:
    from main import MusicBot

log = logging.getLogger(__name__)


class Settings(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot

    def _require_manage_guild(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            raise BotUserError(messages.not_in_guild())
        if not interaction.user.guild_permissions.manage_guild and interaction.user.id not in self.bot.cfg.owner_ids:
            raise BotUserError(messages.manage_server_required())

    # ------------------------------------------------------------- /settings
    settings_group = app_commands.Group(
        name="settings",
        description="Manage server music settings",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @settings_group.command(name="view", description="View current server music settings")
    @app_commands.guild_only()
    async def settings_view(self, interaction: discord.Interaction) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0
        if not await safe_defer(interaction, ephemeral=True):
            return

        try:
            s = await self.bot.storage.get_guild_settings(guild_id)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)
            return

        dj_role_str = f"<@&{s.dj_role_id}>" if s.dj_role_id else "None"
        restrict_str = f"<#{s.restrict_channel_id}>" if s.restrict_channel_id else "None (all channels allowed)"
        voice_247_str = f"<#{s.voice_247_channel_id}>" if s.voice_247_channel_id else "Disabled"
        if s.max_duration > 0:
            max_dur_str = f"{s.max_duration} min"
        elif self.bot.cfg.max_track_seconds > 0:
            max_dur_str = f"{self.bot.cfg.max_track_seconds // 60} min (default)"
        else:
            max_dur_str = "Unlimited"

        if s.max_queue > 0:
            max_q_str = str(s.max_queue)
        elif self.bot.cfg.max_queue_size > 0:
            max_q_str = f"{self.bot.cfg.max_queue_size} (default)"
        else:
            max_q_str = "Unlimited"

        lines = [
            "### Server Settings",
            messages.server_setting_row("DJ Role", dj_role_str, is_mention=bool(s.dj_role_id)),
            messages.server_setting_row("DJ Only", "Enabled" if s.dj_only else "Disabled"),
            messages.server_setting_row("Default Volume", f"{s.default_volume}%"),
            messages.server_setting_row("Volume Limit", f"{s.volume_limit}%"),
            messages.server_setting_row("Max Track Duration", max_dur_str),
            messages.server_setting_row("Max Queue Size", max_q_str),
            messages.server_setting_row("Restricted Channel", restrict_str, is_mention=bool(s.restrict_channel_id)),
            messages.server_setting_row("24/7 Mode", voice_247_str, is_mention=bool(s.voice_247_channel_id)),
            messages.server_setting_row("Restore Queue On Restart", "Enabled" if s.restore_queue else "Disabled"),
        ]
        await reply(interaction, "\n".join(lines), ephemeral=True)

    @settings_group.command(name="djrole", description="Set or clear the DJ role")
    @app_commands.describe(role="Role to set as DJ (leave empty to clear)")
    @app_commands.guild_only()
    async def settings_djrole(self, interaction: discord.Interaction, role: discord.Role | None = None) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0
        if not await safe_defer(interaction, ephemeral=True):
            return

        role_id = role.id if role else 0
        try:
            await self.bot.storage.update_guild_settings(guild_id, dj_role_id=role_id)
            player = self.bot.registry.get(guild_id)
            if player is not None:
                player.dj_role_id = role_id
            if role:
                await reply(interaction, messages.dj_role_set(role_id), ephemeral=True)
            else:
                await reply(interaction, messages.dj_role_set(None), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @settings_group.command(name="dj-only", description="Require DJ role for all playback controls")
    @app_commands.describe(enabled="Whether to enforce DJ role for controls")
    @app_commands.guild_only()
    async def settings_dj_only(self, interaction: discord.Interaction, enabled: bool) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0
        if not await safe_defer(interaction, ephemeral=True):
            return

        try:
            await self.bot.storage.update_guild_settings(guild_id, dj_only=enabled)
            player = self.bot.registry.get(guild_id)
            if player is not None:
                player.dj_only = enabled
            await reply(interaction, messages.dj_only_toggled(enabled), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @settings_group.command(name="volume-limit", description="Set the maximum volume ceiling (1-100)")
    @app_commands.describe(limit="Maximum volume ceiling")
    @app_commands.guild_only()
    async def settings_volume_limit(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 100],
    ) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0
        if not await safe_defer(interaction, ephemeral=True):
            return

        try:
            await self.bot.storage.update_guild_settings(guild_id, volume_limit=limit)
            player = self.bot.registry.get(guild_id)
            if player is not None and player.volume > limit:
                await player.set_volume(limit)
            await reply(interaction, messages.volume_limit_set(limit), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @settings_group.command(name="max-duration", description="Set maximum allowed track duration in minutes (0 disables)")
    @app_commands.describe(minutes="Duration in minutes (0 for default)")
    @app_commands.guild_only()
    async def settings_max_duration(
        self,
        interaction: discord.Interaction,
        minutes: app_commands.Range[int, 0, 1440],
    ) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0
        if not await safe_defer(interaction, ephemeral=True):
            return

        try:
            await self.bot.storage.update_guild_settings(guild_id, max_duration=minutes)
            await reply(interaction, messages.max_duration_set(minutes), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @settings_group.command(name="max-queue", description="Set maximum queue size (0 for unlimited)")
    @app_commands.describe(count="Maximum number of tracks in queue (0 for unlimited)")
    @app_commands.guild_only()
    async def settings_max_queue(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 0, 500000],
    ) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0
        if not await safe_defer(interaction, ephemeral=True):
            return

        try:
            await self.bot.storage.update_guild_settings(guild_id, max_queue=count)
            player = self.bot.registry.get(guild_id)
            if player is not None:
                player.queue.max_size = count if count > 0 else self.bot.cfg.max_queue_size
            await reply(interaction, messages.max_queue_set(count), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @settings_group.command(name="restrict", description="Limit music commands to a specific channel (leave empty to clear)")
    @app_commands.describe(channel="Channel to restrict commands to")
    @app_commands.guild_only()
    async def settings_restrict(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0
        if not await safe_defer(interaction, ephemeral=True):
            return

        ch_id = channel.id if channel else 0
        try:
            await self.bot.storage.update_guild_settings(guild_id, restrict_channel_id=ch_id)
            await reply(interaction, messages.restrict_channel_set(ch_id if channel else None), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @settings_group.command(name="restore-queue", description="Save queue and restore it when bot restarts")
    @app_commands.describe(enabled="Whether to restore queue on startup")
    @app_commands.guild_only()
    async def settings_restore_queue(self, interaction: discord.Interaction, enabled: bool) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0
        if not await safe_defer(interaction, ephemeral=True):
            return

        try:
            await self.bot.storage.update_guild_settings(guild_id, restore_queue=enabled)
            await reply(
                interaction,
                messages.restore_queue_toggled(enabled),
                ephemeral=True,
            )
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    # -------------------------------------------------------- /defaultvolume
    @app_commands.command(name="defaultvolume", description="Set the default starting volume for this server (1-100)")
    @app_commands.describe(volume="Volume level (1-100)")
    @app_commands.guild_only()
    async def default_volume(
        self,
        interaction: discord.Interaction,
        volume: app_commands.Range[int, 1, 100],
    ) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0
        clamped_vol = max(1, min(100, volume))
        if not await safe_defer(interaction, ephemeral=True):
            return

        try:
            await self.bot.storage.update_guild_settings(guild_id, default_volume=clamped_vol)
            await reply(interaction, messages.default_volume_set(clamped_vol), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    # ----------------------------------------------------------------- /247
    @app_commands.command(name="247", description="Toggle 24/7 mode to stay connected without leaving")
    @app_commands.guild_only()
    async def mode_247(self, interaction: discord.Interaction) -> None:
        self._require_manage_guild(interaction)
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            raise BotUserError(messages.not_in_guild())

        guild_id = guild.id
        if not await safe_defer(interaction):
            return

        try:
            s = await self.bot.storage.get_guild_settings(guild_id)
        except StorageError:
            await reply(interaction, messages.storage_unavailable())
            return

        if s.voice_247_channel_id:
            # Currently enabled -> disable it
            try:
                await self.bot.storage.update_guild_settings(guild_id, voice_247_channel_id=0)
            except StorageError:
                await reply(interaction, messages.storage_unavailable())
                return

            player = self.bot.registry.get(guild_id)
            if player is not None:
                player.is_247 = False
            await reply(interaction, messages.mode_247_toggled(False))
        else:
            # Currently disabled -> enable it
            channel = user_voice_channel(interaction)
            if isinstance(channel, discord.StageChannel):
                raise StageUnsupported()
            me = guild.me
            if isinstance(me, discord.Member):
                perms = channel.permissions_for(me)
                if not (perms.view_channel and perms.connect and perms.speak):
                    raise MissingVoicePermissions()

            try:
                await self.bot.storage.update_guild_settings(guild_id, voice_247_channel_id=channel.id)
            except StorageError:
                await reply(interaction, messages.storage_unavailable())
                return

            player = await self.bot.registry.get_or_create(guild_id, channel.id, interaction.channel_id or 0)
            player.is_247 = True
            await reply(interaction, messages.mode_247_toggled(True, channel.id))


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Settings(bot))
