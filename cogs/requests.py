"""Persistent DJ moderated track requests."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from core.queue import QueueItem, extract_user_avatar_url
from core.storage import StorageError, StoredTrack
from utils.checks import guild_member, require_dj, user_voice_channel
from utils.components_v2 import PaginatedPage, PaginatedView, reply_card
from utils.errors import BotUserError, StageUnsupported, WrongChannel
from utils.interaction import reply, safe_defer
from utils.text import clean, format_duration, truncate

if TYPE_CHECKING:
    from main import MusicBot

log = logging.getLogger(__name__)


class Requests(commands.Cog):
    """Let listeners suggest tracks and DJs approve them into the queue."""

    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot

    requests_group = app_commands.Group(name="requests", description="Submit and moderate track requests")

    async def _require_dj(self, interaction: discord.Interaction) -> None:
        member = guild_member(interaction)
        player = self.bot.registry.get(interaction.guild_id or 0)
        settings = await self.bot.storage.get_guild_settings(interaction.guild_id or 0)
        require_dj(
            self.bot,
            self.bot.cfg,
            member,
            dj_role_id=getattr(player, "dj_role_id", 0) or getattr(settings, "dj_role_id", 0),
            dj_only=getattr(player, "dj_only", settings.dj_only),
        )

    @requests_group.command(name="add", description="Submit a song for a DJ to approve")
    @app_commands.describe(query="A song title or link")
    @app_commands.guild_only()
    async def request_add(self, interaction: discord.Interaction, query: app_commands.Range[str, 1, 300]) -> None:
        channel = user_voice_channel(interaction)
        if isinstance(channel, discord.StageChannel):
            raise StageUnsupported()
        guild_id = interaction.guild_id or 0
        player = self.bot.registry.get(guild_id)
        if (
            player
            and player.voice_channel_id != channel.id
            and self.bot.backend.voice_connected(guild_id, player.voice_channel_id)
        ):
            raise WrongChannel(channel_id=player.voice_channel_id)
        if not await safe_defer(interaction, ephemeral=True):
            return
        music = self.bot.get_cog("Music")
        if music is None:
            raise BotUserError("Music search is temporarily unavailable.")
        items, _, _, _ = await music._load_query_items(
            query.strip(), interaction.user.id, guild_id, extract_user_avatar_url(interaction.user)
        )
        if not items:
            raise BotUserError("I couldn't find a playable track for that request.")
        item = items[0]
        stored_query = (
            f"ytsearch:{item.title} - {item.artist}"
            if item.spotify_metadata
            else item.uri or item.query or query.strip()
        )
        stored = StoredTrack(
            uri=stored_query,
            title=clean(item.title)[:200],
            artist=clean(item.artist)[:100],
            duration_ms=item.duration_ms,
            requester_id=interaction.user.id,
        )
        try:
            request_id = await self.bot.storage.add_track_request(guild_id, interaction.user.id, stored)
        except StorageError as exc:
            await reply(interaction, str(exc), ephemeral=True)
            return
        await reply(
            interaction,
            f"Request **#{request_id}** submitted: **{truncate(stored.title)}**. A DJ can review it with `/requests list`.",
            ephemeral=True,
        )

    @requests_group.command(name="list", description="Review pending track requests")
    @app_commands.guild_only()
    async def request_list(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        await self._require_dj(interaction)
        try:
            rows = await self.bot.storage.list_track_requests(interaction.guild_id or 0)
        except StorageError:
            await reply(interaction, "Request storage is temporarily unavailable.", ephemeral=True)
            return
        page_size = 10
        page_count = max(1, (len(rows) + page_size - 1) // page_size)

        def request_page(page_number: int) -> PaginatedPage:
            page_number = min(max(1, page_number), page_count)
            start = (page_number - 1) * page_size
            page_rows = rows[start : start + page_size]
            items = [
                f"**#{rid}** | **{discord.utils.escape_mentions(discord.utils.escape_markdown(truncate(track.title, 70)))}** "
                f"| `{format_duration(track.duration_ms)}` | <@{uid}>"
                for rid, uid, track in page_rows
            ]
            return PaginatedPage(
                title="**Pending Track Requests**",
                items=items,
                current_page=page_number,
                total_pages=page_count,
                empty_message="No pending requests.",
                extra_header="Approve with `/requests approve` or decline with `/requests reject`.",
            )

        view = PaginatedView(
            items_provider=request_page,
            author_id=interaction.user.id,
            guild_id=interaction.guild_id or 0,
            kind="requests",
        )
        await view.render(1)
        await reply_card(interaction, view, ephemeral=True)

    @requests_group.command(name="approve", description="Approve a request and add it to playback")
    @app_commands.describe(request_id="ID shown by /requests list")
    @app_commands.guild_only()
    async def request_approve(
        self, interaction: discord.Interaction, request_id: app_commands.Range[int, 1, 2_147_483_647]
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        await self._require_dj(interaction)
        channel = user_voice_channel(interaction)
        if isinstance(channel, discord.StageChannel):
            raise StageUnsupported()
        guild_id = interaction.guild_id or 0
        existing = self.bot.registry.get(guild_id)
        if (
            existing
            and existing.voice_channel_id != channel.id
            and self.bot.backend.voice_connected(guild_id, existing.voice_channel_id)
        ):
            raise WrongChannel(channel_id=existing.voice_channel_id)
        row = await self.bot.storage.pop_track_request(guild_id, request_id)
        if row is None:
            await reply(interaction, "That request is no longer pending.", ephemeral=True)
            return
        requester_id, track = row
        player = None
        item = QueueItem(
            None,
            track.title,
            track.duration_ms,
            requester_id,
            artist=track.artist,
            uri=track.uri,
            query=track.uri,
        )
        try:
            player = await self.bot.registry.get_or_create(guild_id, channel.id, interaction.channel_id or 0)
            result = await player.enqueue([item])
        except asyncio.CancelledError:
            already_queued = player is not None and (player.current is item or any(queued is item for queued in player.queue))
            if not already_queued:
                await asyncio.shield(self.bot.storage.add_track_request(guild_id, requester_id, track))
            raise
        except Exception:
            already_queued = player is not None and (player.current is item or any(queued is item for queued in player.queue))
            if already_queued:
                log.exception("guild=%s request #%s was queued but playback reported an error", guild_id, request_id)
                await reply(
                    interaction,
                    f"Request **#{request_id}** was added to the queue, but playback hit an error.",
                    ephemeral=True,
                )
                return
            restored_id = await self.bot.storage.add_track_request(guild_id, requester_id, track)
            log.exception("guild=%s failed to enqueue request #%s; restored as #%s", guild_id, request_id, restored_id)
            await reply(
                interaction,
                f"The queue could not accept that track, so the request was restored as **#{restored_id}**.",
                ephemeral=True,
            )
            return
        if result.added:
            safe_title = discord.utils.escape_markdown(truncate(track.title))
            await reply(interaction, f"Approved **#{request_id}** and queued **{safe_title}**.", ephemeral=True)
        else:
            await self.bot.storage.add_track_request(guild_id, requester_id, track)
            await reply(interaction, "The request could not be added to the queue; it remains pending.", ephemeral=True)

    @requests_group.command(name="reject", description="Decline a pending track request")
    @app_commands.describe(request_id="ID shown by /requests list")
    @app_commands.guild_only()
    async def request_reject(
        self, interaction: discord.Interaction, request_id: app_commands.Range[int, 1, 2_147_483_647]
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        await self._require_dj(interaction)
        removed = await self.bot.storage.delete_track_request(interaction.guild_id or 0, request_id)
        await reply(
            interaction,
            f"Request **#{request_id}** declined." if removed else "That request is no longer pending.",
            ephemeral=True,
        )


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Requests(bot))
