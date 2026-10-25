"""Server settings, default volume, and 24/7 mode commands."""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from core.storage import StorageError
from utils import messages
from utils.checks import user_voice_channel
from utils.components_v2 import BaseCardView, TextDisplay, create_card_container, reply_card
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
            messages.server_setting_row("Crash Recovery Queue Checkpoints", "Enabled" if s.restore_queue else "Disabled"),
            messages.server_setting_row("Voice Channel Status", "Enabled" if s.voice_status_enabled else "Disabled"),
            messages.server_setting_row("Auto Pause When Empty", "Enabled" if s.auto_pause else "Disabled"),
        ]
        text_disp = TextDisplay("\n".join(lines))
        container = create_card_container(text_disp)
        view = BaseCardView(timeout=60.0, author_id=interaction.user.id)
        view.add_item(container)
        await reply_card(interaction, view, ephemeral=True)

    @app_commands.command(name="setup-wizard", description="Set the main DJ, command channel, and starting volume")
    @app_commands.describe(
        dj_role="Optional role that controls DJ-only features",
        command_channel="Optional channel for music commands",
        default_volume="Optional starting volume from 1 to 100",
    )
    @app_commands.guild_only()
    async def setup_wizard(
        self,
        interaction: discord.Interaction,
        dj_role: discord.Role | None = None,
        command_channel: discord.TextChannel | None = None,
        default_volume: app_commands.Range[int, 1, 100] | None = None,
    ) -> None:
        self._require_manage_guild(interaction)
        fields: dict[str, int] = {}
        if dj_role is not None:
            fields["dj_role_id"] = dj_role.id
        if command_channel is not None:
            fields["restrict_channel_id"] = command_channel.id
        if default_volume is not None:
            fields["default_volume"] = default_volume
        if not fields:
            await reply(interaction, "Choose at least one setup option to update.", ephemeral=True)
            return
        try:
            settings = await self.bot.storage.update_guild_settings(interaction.guild_id or 0, **fields)
            player = self.bot.registry.get(interaction.guild_id or 0)
            if player is not None:
                player.dj_role_id = settings.dj_role_id
                if player.current is None:
                    player.volume = settings.default_volume
            lines = ["### Setup Updated"]
            if dj_role is not None:
                lines.append(f"DJ role: <@&{dj_role.id}>")
            if command_channel is not None:
                lines.append(f"Music command channel: <#{command_channel.id}>")
            if default_volume is not None:
                lines.append(f"Starting volume: `{default_volume}%`")
            await reply(interaction, "\n".join(lines), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @app_commands.command(name="permission-check", description="Check Melora's permissions in this server")
    @app_commands.guild_only()
    async def permission_check(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await reply(interaction, messages.not_in_guild(), ephemeral=True)
            return
        checks = {
            "View channel": interaction.app_permissions.view_channel,
            "Send messages": interaction.app_permissions.send_messages,
            "Embed links": interaction.app_permissions.embed_links,
        }
        voice = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if voice is not None and guild.me is not None:
            voice_permissions = voice.permissions_for(guild.me)
            checks.update({"Connect to voice": voice_permissions.connect, "Speak": voice_permissions.speak})
        missing = [name for name, allowed in checks.items() if not allowed]
        if missing:
            result = "Missing permissions: " + ", ".join(f"**{name}**" for name in missing)
        else:
            result = "Melora has the required permissions for this channel."
        await reply(interaction, result, ephemeral=True)

    @settings_group.command(name="djrole", description="Set or clear the DJ role")
    @app_commands.describe(role="Role to set as DJ (leave empty to clear)")
    @app_commands.guild_only()
    async def settings_djrole(self, interaction: discord.Interaction, role: discord.Role | None = None) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0

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

        try:
            await self.bot.storage.update_guild_settings(guild_id, max_duration=minutes)
            await reply(interaction, messages.max_duration_set(minutes), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @settings_group.command(name="max-queue", description="Set maximum queue size (0 uses the bot default)")
    @app_commands.describe(count="Maximum tracks in this server queue (0 uses the bot default)")
    @app_commands.guild_only()
    async def settings_max_queue(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 0, 10000],
    ) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0

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

        ch_id = channel.id if channel else 0
        try:
            await self.bot.storage.update_guild_settings(guild_id, restrict_channel_id=ch_id)
            await reply(interaction, messages.restrict_channel_set(ch_id if channel else None), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @settings_group.command(name="restore-queue", description="Save queue checkpoints for crash recovery")
    @app_commands.describe(enabled="Whether to keep periodic queue checkpoints for crash recovery")
    @app_commands.guild_only()
    async def settings_restore_queue(self, interaction: discord.Interaction, enabled: bool) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0

        try:
            await self.bot.storage.update_guild_settings(guild_id, restore_queue=enabled)
            player = self.bot.registry.get(guild_id)
            if player is not None:
                player.restore_queue_enabled = enabled
                if not enabled:
                    player.cancel_timer("snapshot")
            if not enabled:
                # Disabling restore must also discard an older on-disk snapshot.
                await self.bot.storage.delete_queue_snapshot(guild_id)
            await reply(
                interaction,
                messages.restore_queue_toggled(enabled),
                ephemeral=True,
            )
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @settings_group.command(name="voice-status", description="Enable or disable voice channel status updates")
    @app_commands.describe(enabled="Whether to show player status in the voice channel")
    @app_commands.guild_only()
    async def settings_voice_status(self, interaction: discord.Interaction, enabled: bool) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0

        try:
            await self.bot.storage.update_guild_settings(guild_id, voice_status_enabled=enabled)
            voice_status = getattr(self.bot, "voice_status", None)
            if voice_status is not None:
                if not enabled:
                    await voice_status.on_setting_disabled(guild_id)
                else:
                    await voice_status.on_setting_enabled(guild_id)
            await reply(
                interaction,
                messages.voice_status_toggled(enabled),
                ephemeral=True,
            )
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @settings_group.command(
        name="auto-pause", description="Pause when the voice channel empties and resume when listeners return"
    )
    @app_commands.describe(enabled="Whether to pause on empty and resume when someone rejoins")
    @app_commands.guild_only()
    async def settings_auto_pause(self, interaction: discord.Interaction, enabled: bool) -> None:
        self._require_manage_guild(interaction)
        guild_id = interaction.guild_id or 0
        try:
            await self.bot.storage.update_guild_settings(guild_id, auto_pause=enabled)
            player = self.bot.registry.get(guild_id)
            if player is not None:
                player.auto_pause = enabled
                if not enabled:
                    player.auto_paused = False
            await reply(interaction, f"Auto-pause {'enabled' if enabled else 'disabled'}.", ephemeral=True)
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

        try:
            await self.bot.storage.update_guild_settings(guild_id, default_volume=clamped_vol)
            await reply(interaction, messages.default_volume_set(clamped_vol), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @app_commands.command(name="guest-dj", description="Temporarily grant DJ controls to a member")
    @app_commands.describe(member="Member to grant DJ controls", minutes="Grant duration (5 to 480 minutes)")
    @app_commands.guild_only()
    async def guest_dj(
        self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 5, 480]
    ) -> None:
        self._require_manage_guild(interaction)
        grants = self.bot.temporary_dj_grants
        grants[(member.guild.id, member.id)] = time.monotonic() + minutes * 60
        await reply(interaction, f"<@{member.id}> has DJ controls for **{minutes} minutes**.", ephemeral=True)

    @app_commands.command(name="guest-dj-revoke", description="Revoke a member's temporary DJ controls")
    @app_commands.describe(member="Member to revoke")
    @app_commands.guild_only()
    async def guest_dj_revoke(self, interaction: discord.Interaction, member: discord.Member) -> None:
        self._require_manage_guild(interaction)
        removed = self.bot.temporary_dj_grants.pop((member.guild.id, member.id), None) is not None
        text = f"Temporary DJ access removed for <@{member.id}>." if removed else "That member has no temporary DJ grant."
        await reply(interaction, text, ephemeral=True)

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
