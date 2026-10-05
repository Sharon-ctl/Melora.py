"""Administration and informational slash commands with Components V2 cards."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Select

from core.data_loader import load_help_categories
from core.stats import collect_stats, rss_mb
from utils.checks import owner_only
from utils.components_v2 import (
    ActionRow,
    BaseCardView,
    Button,
    PaginatedPage,
    PaginatedView,
    TextDisplay,
    create_card_container,
    reply_card,
    secondary_button,
)
from utils import messages
from utils.errors import BotUserError, ERROR_BUFFER
from utils.interaction import reply, safe_defer
from utils.text import format_uptime

if TYPE_CHECKING:
    from main import MusicBot

log = logging.getLogger(__name__)

SYNC_TIMEOUT = 60.0


class ResetConfirmView(BaseCardView):
    """Components V2 confirmation card to reset user data."""

    def __init__(self, cog: Admin, user_id: int) -> None:
        super().__init__(timeout=60.0, author_id=user_id, kind="reset")
        self.cog = cog
        self.confirmed = False

        text_disp = TextDisplay(messages.reset_prompt())

        btn_confirm = secondary_button("Confirm Delete")
        btn_confirm.callback = self._on_confirm

        btn_cancel = secondary_button("Cancel")
        btn_cancel.callback = self._on_cancel

        row = ActionRow(btn_confirm, btn_cancel)
        container = create_card_container(text_disp, row)
        self.add_item(container)

    def release_references(self) -> None:
        super().release_references()
        self.cog = None  # type: ignore

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                messages.not_your_menu(),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self.confirmed = True
        cog = self.cog
        await self.disable_and_stop()
        storage = getattr(cog.bot, "storage", None) if cog else None
        if storage is not None:
            try:
                await storage.reset_user_data(self.author_id)
                await interaction.followup.send(
                    messages.reset_confirmed(),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception as exc:
                log.warning("user=%s reset failed: %s", self.author_id, exc)
                await interaction.followup.send(
                    messages.storage_unavailable(),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        else:
            await interaction.followup.send(
                messages.storage_unavailable(),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                messages.not_your_menu(),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.disable_and_stop()
        await interaction.followup.send(
            messages.reset_cancelled(),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


HELP_PAGE_SIZE = 10


def precompute_help_pages(
    categories: list[str],
    descriptions: dict[str, str],
    commands_by_cat: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Precompute all formatted help pages for each category to ensure zero runtime re-renders."""
    pages_by_cat: dict[str, list[str]] = {}
    for cat in categories:
        if cat == "Overview":
            lines = [
                "**Melora Help - Overview**",
                "A slash-command music bot for Discord.\n",
                "Tips:",
                "- Select a category from the dropdown menu to browse commands.",
                "- Autocomplete assists with queue positions, playlists, and search queries.",
                "- Spotify track, album, and playlist links work directly in /play.",
                "- Audio node and gateway health can be viewed anytime with /status.",
                "\nPage 1 of 1",
            ]
            pages_by_cat["Overview"] = ["\n".join(lines)]
        else:
            desc = descriptions.get(cat, "Command reference")
            cmds = commands_by_cat.get(cat, [])
            total_cmds = len(cmds)
            total_pages = max(1, math.ceil(total_cmds / HELP_PAGE_SIZE))
            cat_pages: list[str] = []
            for p in range(1, total_pages + 1):
                lines = [f"**Melora Help - {cat}**", f"{desc}\n"]
                start = (p - 1) * HELP_PAGE_SIZE
                page_cmds = cmds[start : start + HELP_PAGE_SIZE]
                if page_cmds:
                    lines.extend(page_cmds)
                else:
                    lines.append("No commands available in this category.")
                lines.append(f"\nPage {p} of {total_pages}")
                cat_pages.append("\n".join(lines))
            pages_by_cat[cat] = cat_pages
    return pages_by_cat


class HelpView(BaseCardView):
    """Components V2 Help card with category dropdown and pagination."""

    def __init__(
        self,
        categories: list[str],
        descriptions: dict[str, str],
        commands_by_cat: dict[str, list[str]],
        author_id: int,
        timeout: float = 120.0,
        pages_by_cat: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(timeout=timeout, author_id=author_id, kind="help")
        self.categories = categories
        self.descriptions = descriptions
        self.commands_by_cat = commands_by_cat
        self.pages_by_cat = (
            pages_by_cat
            if pages_by_cat is not None
            else precompute_help_pages(categories, descriptions, commands_by_cat)
        )
        self.selected_category = "Overview"
        self.current_page = 1
        self.total_pages = 1
        self._select: Select[Any] | None = None
        self._prev_btn: Button[Any] | None = None
        self._next_btn: Button[Any] | None = None

    def release_references(self) -> None:
        super().release_references()
        self.pages_by_cat = {}
        self.commands_by_cat = {}

    async def render(self) -> None:
        self.clear_items()
        cat_pages = self.pages_by_cat.get(self.selected_category)
        if not cat_pages:
            cat_pages = [f"**Melora Help - {self.selected_category}**\n\nNo commands available in this category.\n\nPage 1 of 1"]
        self.total_pages = len(cat_pages)
        self.current_page = max(1, min(self.current_page, self.total_pages))
        page_text = cat_pages[self.current_page - 1]

        text_disp = TextDisplay(page_text)

        options = [
            discord.SelectOption(
                label=cat,
                value=cat,
                description=self.descriptions.get(cat, "")[:50],
                default=(cat == self.selected_category),
            )
            for cat in self.categories
        ]
        self._select = Select(options=options)
        self._select.callback = self._on_select
        select_row = ActionRow(self._select)

        self._prev_btn = secondary_button("<", disabled=(self.current_page <= 1), callback=self._on_prev)
        self._next_btn = secondary_button(">", disabled=(self.current_page >= self.total_pages), callback=self._on_next)
        btn_row = ActionRow(self._prev_btn, self._next_btn)

        container = create_card_container(text_disp, select_row, btn_row)
        self.add_item(container)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                messages.not_your_menu(),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if self._select and self._select.values:
            self.selected_category = self._select.values[0]
            self.current_page = 1
            await self.render()
            await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.defer()

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                messages.not_your_menu(),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if self.current_page > 1:
            self.current_page -= 1
            await self.render()
            await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.defer()

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                messages.not_your_menu(),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if self.current_page < self.total_pages:
            self.current_page += 1
            await self.render()
            await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.defer()

    async def on_timeout(self) -> None:
        await self.disable_and_stop()


class Admin(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        self._help_cache: dict[str, list[str]] = {}
        self._help_categories: list[str] = []
        self._help_descriptions: dict[str, str] = {}
        self._help_pages: dict[str, list[str]] = {}
        self.rebuild_help_cache()

    def rebuild_help_cache(self) -> None:
        meta = load_help_categories()
        mapping = meta.get("mapping", {})
        descriptions = meta.get("descriptions", {})
        cats = list(
            meta.get(
                "categories",
                ["Overview", "Playback", "Queue", "Library", "Filters", "Settings", "Info", "Owner"],
            )
        )

        by_cat: dict[str, list[str]] = {c: [] for c in cats}
        by_cat["Other"] = []

        all_cmds = sorted(self.bot.tree.get_commands(), key=lambda c: c.name)
        for cmd in all_cmds:
            if isinstance(cmd, app_commands.Group):
                subcmds = sorted(cmd.commands, key=lambda s: s.name)
                for sub in subcmds:
                    full_name = f"{cmd.name} {sub.name}"
                    desc = sub.description or "No description."
                    cat = mapping.get(full_name, "Other")
                    if cat not in by_cat:
                        by_cat[cat] = []
                    by_cat[cat].append(f"**/{full_name}** • {desc}")
            else:
                desc = cmd.description or "No description."
                cat = mapping.get(cmd.name, "Other")
                if cat not in by_cat:
                    by_cat[cat] = []
                by_cat[cat].append(f"**/{cmd.name}** • {desc}")

        if not by_cat["Other"]:
            del by_cat["Other"]

        self._help_cache = by_cat
        self._help_categories = [c for c in cats if c == "Overview" or (c in by_cat and by_cat[c])]
        if "Other" in by_cat and "Other" not in self._help_categories:
            self._help_categories.append("Other")
        self._help_descriptions = descriptions
        self._help_pages = precompute_help_pages(self._help_categories, descriptions, by_cat)

    @app_commands.command(name="sync", description="Owner only: sync slash commands with Discord")
    @owner_only()
    async def sync(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        try:
            synced = await asyncio.wait_for(self.bot.tree.sync(), timeout=SYNC_TIMEOUT)
        except (discord.HTTPException, asyncio.TimeoutError):
            log.exception("Command sync failed")
            raise BotUserError(messages.sync_failed()) from None
        log.info("Synced %d commands on request", len(synced))
        self.rebuild_help_cache()
        await reply(interaction, messages.commands_synced(len(synced)), ephemeral=True)

    @app_commands.command(name="status", description="Show bot and runtime status")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction):
            return

        stats = collect_stats(self.bot)
        latency = f"{stats.latency_ms} ms" if stats.latency_ms >= 0 else "unknown"
        uptime = format_uptime(stats.uptime_seconds)

        lines: list[str] = [
            "### Melora Status",
            "",
            f"**Gateway Latency:** `{latency}`",
            f"**Uptime:** `{uptime}`",
            f"**Active Players:** `{stats.players}`",
            "",
            "### Gateway Shards",
        ]

        shards = getattr(self.bot, "shards", None)
        if shards:
            guild_counts: dict[int, int] = {}
            for g in self.bot.guilds:
                s_id = getattr(g, "shard_id", 0)
                guild_counts[s_id] = guild_counts.get(s_id, 0) + 1
            for shard_id, shard in sorted(shards.items()):
                lat = (
                    f"{shard.latency * 1000:.1f} ms"
                    if (shard.latency is not None and not math.isnan(shard.latency))
                    else "unknown"
                )
                state = "disconnected" if shard.is_closed() else "connected"
                cnt = guild_counts.get(shard_id, 0)
                lines.append(f"**Shard {shard_id}:** `{state}` • `{lat}` • `{cnt} guilds`")
        else:
            lines.append(f"**Shard 0:** `connected` • `{latency}` • `{len(self.bot.guilds)} guilds`")

        lines.append("")
        lines.append("### Lavalink Nodes")

        lavalink_client = getattr(self.bot, "lavalink", None)
        nodes = getattr(getattr(lavalink_client, "node_manager", None), "nodes", []) if lavalink_client else []
        available_nodes = [n for n in nodes if getattr(n, "available", False)]
        if not nodes:
            lines.append("**Nodes:** `None configured`")
        elif not available_nodes:
            lines.append("**Status:** `Music server offline`")
            for node in nodes:
                lines.append(f"**{node.name}:** `offline` • `Players: {len(getattr(node, 'players', []))}`")
        else:
            for node in nodes:
                state = "online" if node.available else "offline"
                n_stats = getattr(node, "stats", None)
                if n_stats and not getattr(n_stats, "is_fake", False):
                    cpu = f"{n_stats.lavalink_load * 100:.1f}%"
                    mem = f"{n_stats.memory_used / (1024 * 1024):.1f} MB"
                    p_info = f"{n_stats.playing_players}/{n_stats.players}"
                    lines.append(f"**{node.name}:** `{state}` • `CPU: {cpu}` • `Mem: {mem}` • `Players: {p_info}`")
                else:
                    lines.append(f"**{node.name}:** `{state}` • `Players: {len(getattr(node, 'players', []))}`")

        # Voice bitrate analysis
        lines.append("")
        lines.append("### Voice Bitrate")
        guild = interaction.guild
        if guild is not None:
            max_kbps = guild.bitrate_limit // 1000
            player = self.bot.registry.get(guild.id)
            vc_channel: discord.VoiceChannel | discord.StageChannel | None = None
            if player is not None:
                ch = guild.get_channel(player.voice_channel_id)
                if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                    vc_channel = ch
            if vc_channel is None and isinstance(interaction.user, discord.Member) and interaction.user.voice:
                ch = interaction.user.voice.channel
                if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                    vc_channel = ch

            if vc_channel is not None:
                cur_kbps = vc_channel.bitrate // 1000
                status_qual = "degraded" if cur_kbps < max_kbps else "optimal"
                lines.append(f"**Channel {vc_channel.name}:** `{cur_kbps} kbps` • `Max {max_kbps} kbps ({status_qual})`")
            else:
                lines.append(f"**Server Max:** `{max_kbps} kbps` • `Not connected`")

        # Owner-only diagnostic section
        is_owner = interaction.user.id in self.bot.cfg.owner_ids
        if not is_owner:
            try:
                is_owner = await self.bot.is_owner(interaction.user)
            except Exception as exc:
                log.debug("is_owner check failed: %s", exc)

        if is_owner:
            lines.append("")
            lines.append("### Diagnostics (Owner)")
            mem = rss_mb()
            lines.append(f"**Process Memory:** `{mem:.1f} MB`")
            try:
                task_count = len(asyncio.all_tasks())
            except RuntimeError as exc:
                log.debug("all_tasks query failed: %s", exc)
                task_count = 0
            supervised = self.bot.supervisor.live_count()
            lines.append(f"**Tasks:** `{task_count} total` • `{supervised} supervised`")
            lines.append(f"**Registry Size:** `{len(self.bot.registry)}`")

            loop_monitor = getattr(getattr(self.bot, "registry", None), "loop_monitor", None)
            if loop_monitor is not None:
                st = loop_monitor.stats()
                shed_str = " • `Load Shedding: ACTIVE`" if loop_monitor.is_load_shedding else ""
                lines.append(
                    f"**Loop Lag:** `p50: {st['p50']:.1f}ms` • `p95: {st['p95']:.1f}ms` • "
                    f"`p99: {st['p99']:.1f}ms` • `max: {st['max']:.1f}ms`{shed_str}"
                )

            cache_parts: list[str] = []
            music_cog = self.bot.get_cog("Music")
            if music_cog is not None and hasattr(music_cog, "_suggest_cache"):
                cache_parts.append(f"autocomplete={len(music_cog._suggest_cache)}")
            if hasattr(self.bot, "spotify_cache"):
                cache_parts.append(f"spotify={len(self.bot.spotify_cache)}")
            lines.append(f"**Caches:** `{', '.join(cache_parts) if cache_parts else 'none'}`")

            from utils.timing import get_command_timing_stats, get_http_429_count

            http_429s = get_http_429_count()
            lines.append(f"**Discord 429s:** `{http_429s}`")

            timing_stats = get_command_timing_stats()
            if timing_stats:
                lines.append("")
                lines.append("### Timing Percentiles (p50 / p95)")
                for cmd, s in sorted(timing_stats.items()):
                    ack_p = f"{s['ack_p50_ms']}/{s['ack_p95_ms']}ms"
                    tot_p = f"{s['total_p50_ms']}/{s['total_p95_ms']}ms"
                    lines.append(f"**/{cmd}:** `ack {ack_p}` • `total {tot_p}`")

        text_disp = TextDisplay("\n".join(lines))
        container = create_card_container(text_disp)
        view = BaseCardView(timeout=60.0, author_id=interaction.user.id)
        view.add_item(container)
        await reply_card(interaction, view)

    @app_commands.command(name="help", description="Show all available commands")
    @app_commands.guild_only()
    async def help(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction):
            return

        is_owner = interaction.user.id in self.bot.cfg.owner_ids
        if not is_owner:
            try:
                is_owner = await self.bot.is_owner(interaction.user)
            except Exception as exc:
                log.debug("is_owner check failed: %s", exc)

        allowed_cats = [c for c in self._help_categories if c != "Owner" or is_owner]
        view = HelpView(
            categories=allowed_cats,
            descriptions=self._help_descriptions,
            commands_by_cat=self._help_cache,
            author_id=interaction.user.id,
            timeout=120.0,
            pages_by_cat=self._help_pages,
        )
        await view.render()
        await reply_card(interaction, view)

    @app_commands.command(name="errors", description="Owner only: show recent unhandled error logs")
    @app_commands.describe(count="Number of errors to show (default 10, max 50)")
    @owner_only()
    async def errors(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 50] = 10) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        def errors_provider(page_num: int) -> PaginatedPage:
            records = ERROR_BUFFER.get_recent(count)
            total = len(records)
            pages = max(1, math.ceil(total / 10))
            page = max(1, min(page_num, pages))
            start = (page - 1) * 10
            items = [
                f"**[{r.error_id}]** `/{r.command}` at {time.strftime('%H:%M:%S', time.gmtime(r.timestamp))} UTC: {r.summary}"
                for r in records[start : start + 10]
            ]
            return PaginatedPage(
                title=f"**Recent Errors** ({total} recorded)",
                items=items,
                current_page=page,
                total_pages=pages,
                empty_message="No recent errors recorded.",
            )

        view = PaginatedView(
            items_provider=errors_provider,
            author_id=interaction.user.id,
            guild_id=interaction.guild_id or 0,
            kind="errors",
        )
        await view.render(1)
        await reply_card(interaction, view, ephemeral=True)

    @app_commands.command(name="privacy", description="Learn what data Melora stores and why")
    @app_commands.guild_only()
    async def privacy(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        await reply(interaction, messages.privacy_policy(), ephemeral=True)

    @app_commands.command(name="reset", description="Delete all your stored data (favorites and playlists)")
    @app_commands.guild_only()
    async def reset(self, interaction: discord.Interaction) -> None:
        view = ResetConfirmView(self, interaction.user.id)
        await reply_card(interaction, view, ephemeral=True)


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Admin(bot))
