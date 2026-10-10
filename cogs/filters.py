"""Audio filter and equalizer slash commands."""
from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from core.data_loader import load_eq_presets, load_filter_presets
from core.guild_player import GuildPlayer
from utils import messages
from utils.checks import guild_member, require_dj
from utils.components_v2 import PaginatedPage, PaginatedView, reply_card
from utils.errors import BotUserError, NotInVoice, NothingPlaying, WrongChannel
from utils.interaction import reply
from utils.text import clean

if TYPE_CHECKING:
    from main import MusicBot

log = logging.getLogger(__name__)


class Filters(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        try:
            self.eq_presets = load_eq_presets()
        except Exception as exc:
            log.warning("Could not load eq_presets: %s", exc)
            self.eq_presets = {}

        try:
            self.filter_presets = load_filter_presets()
        except Exception as exc:
            log.warning("Could not load filter_presets: %s", exc)
            self.filter_presets = {}

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
        member = guild_member(interaction)
        player = self.bot.registry.get(interaction.guild_id or 0)
        if player is None:
            raise NothingPlaying()
        voice = member.voice
        if voice is None or voice.channel is None:
            raise NotInVoice()
        if voice.channel.id != player.voice_channel_id:
            raise WrongChannel()
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

    # ---------------------------------------------------------------------- /eq
    eq_group = app_commands.Group(name="eq", description="Equalizer commands")

    @eq_group.command(name="preset", description="Apply an equalizer preset")
    @app_commands.describe(name="Preset name")
    @app_commands.guild_only()
    async def eq_preset(self, interaction: discord.Interaction, name: str) -> None:
        player = self._control_gate(interaction, dj=True)
        preset_key = name.strip().lower()
        if preset_key not in self.eq_presets:
            raise BotUserError(messages.unknown_preset(name, list(self.eq_presets.keys())))

        bands = self.eq_presets[preset_key]
        await player.set_eq(preset_key, bands)
        await reply(interaction, messages.eq_preset_applied(preset_key))

    @eq_preset.autocomplete("name")
    async def eq_preset_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            if not interaction.guild or not interaction.guild_id:
                return []
            player = self.bot.registry.get(interaction.guild_id)
            if player is None:
                return []
            current_lower = current.strip().lower()
            matches = [name for name in self.eq_presets if current_lower in name.lower()]
            return [app_commands.Choice(name=clean(name)[:100], value=name) for name in matches[:25]]
        except Exception:
            return []

    @eq_group.command(name="reset", description="Reset equalizer to flat")
    @app_commands.guild_only()
    async def eq_reset(self, interaction: discord.Interaction) -> None:
        player = self._control_gate(interaction, dj=True)
        await player.reset_eq()
        await reply(interaction, messages.eq_reset())

    @eq_group.command(name="list", description="List available equalizer presets")
    @app_commands.guild_only()
    async def eq_list(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id or 0

        def eq_list_provider(page_num: int) -> PaginatedPage:
            player = self.bot.registry.get(guild_id)
            current_eq = player.current_eq if player else None
            eq_str = current_eq or "Flat (default)"
            extra = f"Active EQ: {eq_str}\n"

            presets = sorted(self.eq_presets.keys())
            total = len(presets)
            pages = max(1, math.ceil(total / 10))
            page = max(1, min(page_num, pages))
            start = (page - 1) * 10
            items = [f"{start + i + 1}. **{p}**" for i, p in enumerate(presets[start : start + 10])]
            return PaginatedPage(
                title=f"**Equalizer Presets** ({total} available)",
                items=items,
                current_page=page,
                total_pages=pages,
                extra_header=extra,
                empty_message="No equalizer presets available.",
            )

        view = PaginatedView(
            items_provider=eq_list_provider,
            author_id=interaction.user.id,
            guild_id=interaction.guild_id or 0,
            kind="eq_list",
        )
        await view.render(1)
        await reply_card(interaction, view)


    # ------------------------------------------------------------------ /filter
    filter_group = app_commands.Group(name="filter", description="Audio filter commands")

    @filter_group.command(name="add", description="Add an audio filter effect")
    @app_commands.describe(name="Filter effect name")
    @app_commands.guild_only()
    async def filter_add(self, interaction: discord.Interaction, name: str) -> None:
        player = self._control_gate(interaction, dj=True)
        filter_key = name.strip().lower()
        if filter_key not in self.filter_presets:
            raise BotUserError(messages.unknown_preset(name, list(self.filter_presets.keys())))

        config = self.filter_presets[filter_key]
        await player.apply_filter(filter_key, config)
        await reply(interaction, messages.filter_added(filter_key))

    @filter_add.autocomplete("name")
    async def filter_add_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            if not interaction.guild or not interaction.guild_id:
                return []
            player = self.bot.registry.get(interaction.guild_id)
            if player is None:
                return []
            current_lower = current.strip().lower()
            matches = [name for name in self.filter_presets if current_lower in name.lower()]
            return [app_commands.Choice(name=clean(name)[:100], value=name) for name in matches[:25]]
        except Exception:
            return []

    @filter_group.command(name="remove", description="Remove an audio filter effect")
    @app_commands.describe(name="Filter effect name")
    @app_commands.guild_only()
    async def filter_remove(self, interaction: discord.Interaction, name: str) -> None:
        player = self._control_gate(interaction, dj=True)
        filter_key = name.strip().lower()
        if filter_key not in self.filter_presets:
            raise BotUserError(messages.unknown_preset(name, list(self.filter_presets.keys())))

        if filter_key not in player.applied_filters:
            raise BotUserError(messages.filter_not_active(filter_key))

        config = self.filter_presets[filter_key]
        await player.remove_filter(filter_key, config)
        await reply(interaction, messages.filter_removed(filter_key))

    @filter_remove.autocomplete("name")
    async def filter_remove_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            if not interaction.guild or not interaction.guild_id:
                return []
            player = self.bot.registry.get(interaction.guild_id)
            if player is None:
                return []
            active = player.applied_filters
            current_lower = current.strip().lower()
            matches = [name for name in active if current_lower in name.lower()]
            return [app_commands.Choice(name=clean(name)[:100], value=name) for name in matches[:25]]
        except Exception:
            return []

    @filter_group.command(name="reset", description="Reset all audio filters")
    @app_commands.guild_only()
    async def filter_reset(self, interaction: discord.Interaction) -> None:
        player = self._control_gate(interaction, dj=True)
        await player.reset_filters()
        await reply(interaction, messages.filters_reset())

    @filter_group.command(name="list", description="List available filter presets and active filters")
    @app_commands.guild_only()
    async def filter_list(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id or 0

        def filter_list_provider(page_num: int) -> PaginatedPage:
            player = self.bot.registry.get(guild_id)
            active_filters = sorted(player.applied_filters) if player else []
            active_str = ", ".join(active_filters) if active_filters else "None"
            extra = f"Active Filters: {active_str}\n"

            presets = sorted(self.filter_presets.keys())
            total = len(presets)
            pages = max(1, math.ceil(total / 10))
            page = max(1, min(page_num, pages))
            start = (page - 1) * 10
            items = [f"{start + i + 1}. **{p}**" for i, p in enumerate(presets[start : start + 10])]
            return PaginatedPage(
                title=f"**Filter Presets** ({total} available)",
                items=items,
                current_page=page,
                total_pages=pages,
                extra_header=extra,
                empty_message="No filter presets available.",
            )

        view = PaginatedView(
            items_provider=filter_list_provider,
            author_id=interaction.user.id,
            guild_id=interaction.guild_id or 0,
            kind="filter_list",
        )
        await view.render(1)
        await reply_card(interaction, view)

    @app_commands.command(name="speed", description="Set playback speed (e.g. 0.98 for smooth, 1.0 for normal)")
    @app_commands.describe(speed="Speed multiplier from 0.5 to 2.0 (default 0.98)")
    @app_commands.guild_only()
    async def speed(
        self,
        interaction: discord.Interaction,
        speed: app_commands.Range[float, 0.5, 2.0] = 0.98,
    ) -> None:
        player = self._control_gate(interaction, dj=True)
        await player.set_speed(speed)
        if abs(speed - 1.0) < 0.01:
            await reply(interaction, messages.speed_reset())
        else:
            await reply(interaction, messages.speed_set(speed))


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Filters(bot))
