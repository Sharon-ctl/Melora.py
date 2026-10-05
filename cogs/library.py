"""Personal library commands: favorites and playlists."""
from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from core.queue import QueueItem, extract_user_avatar_url
from core.spotify import parse_spotify_url
from core.storage import StorageError, StoredTrack
from utils import messages
from utils.cache import TTLCache
from utils.checks import check_bot_channel_permissions, user_voice_channel
from utils.components_v2 import PaginatedPage, PaginatedView, reply_card
from utils.errors import BotUserError, NothingPlaying, StageUnsupported, WrongChannel
from utils.interaction import reply, safe_defer
from utils.text import clean, format_duration, truncate

if TYPE_CHECKING:
    from main import MusicBot

log = logging.getLogger(__name__)


class Library(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        self._fav_cache: TTLCache[int, list[StoredTrack]] = TTLCache(max_size=200, ttl=10.0)
        self._playlists_cache: TTLCache[int, list[str]] = TTLCache(max_size=200, ttl=10.0)
        self._tracks_cache: TTLCache[tuple[int, str], list[StoredTrack]] = TTLCache(max_size=200, ttl=10.0)

    async def _get_user_favorites(self, user_id: int) -> list[StoredTrack]:
        cached = self._fav_cache.get(user_id)
        if cached is not None:
            return cached
        tracks = await asyncio.wait_for(self.bot.storage.get_favorites(user_id), timeout=2.0)
        self._fav_cache.set(user_id, list(tracks))
        return list(tracks)

    async def _get_user_playlists(self, user_id: int) -> list[str]:
        cached = self._playlists_cache.get(user_id)
        if cached is not None:
            return cached
        names = await asyncio.wait_for(self.bot.storage.list_playlists(user_id), timeout=2.0)
        self._playlists_cache.set(user_id, list(names))
        return list(names)

    async def _get_user_playlist_tracks(self, user_id: int, playlist_name: str) -> list[StoredTrack]:
        key = (user_id, playlist_name.lower())
        cached = self._tracks_cache.get(key)
        if cached is not None:
            return cached
        tracks = await asyncio.wait_for(self.bot.storage.get_playlist_tracks(user_id, playlist_name), timeout=2.0)
        self._tracks_cache.set(key, list(tracks))
        return list(tracks)

    async def _playlist_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            if not interaction.guild or not interaction.guild_id:
                return []
            user_id = interaction.user.id if interaction.user else 0
            if not user_id:
                return []
            names = await self._get_user_playlists(user_id)
            if not names:
                return []
            typed = str(current).strip().lower()
            matches: list[app_commands.Choice[str]] = []
            for name in names:
                if typed and typed not in name.lower():
                    continue
                label = clean(name)[:100]
                matches.append(app_commands.Choice(name=label, value=name))
                if len(matches) >= 25:
                    break
            return matches
        except Exception:
            return []

    async def _resolve_voice(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Member, discord.VoiceChannel | discord.StageChannel]:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            raise BotUserError(messages.not_in_guild())
        channel = user_voice_channel(interaction)
        if isinstance(channel, discord.StageChannel):
            raise StageUnsupported()
        existing = self.bot.registry.get(guild.id)
        if (
            existing is not None
            and existing.voice_channel_id != channel.id
            and self.bot.backend.voice_connected(guild.id, existing.voice_channel_id)
        ):
            raise WrongChannel()
        check_bot_channel_permissions(interaction, voice_channel=channel)
        return interaction.user, channel

    # ------------------------------------------------------------- favorites
    favorites_group = app_commands.Group(name="favorites", description="Manage personal favorite tracks")

    @favorites_group.command(name="list", description="List your favorite tracks")
    @app_commands.guild_only()
    async def favorites_list(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        user_id = interaction.user.id

        async def favorites_provider(page_num: int) -> PaginatedPage:
            try:
                tracks = await self.bot.storage.get_favorites(user_id)
            except StorageError:
                return PaginatedPage(
                    title="**Your Favorites**",
                    items=[],
                    current_page=1,
                    total_pages=1,
                    empty_message="Storage is temporarily unavailable.",
                )
            total = len(tracks)
            pages = max(1, math.ceil(total / 10))
            page = max(1, min(page_num, pages))
            start = (page - 1) * 10
            items = [
                f"{start + i + 1}. **{messages.escape_subject(t.title)}** • `{format_duration(t.duration_ms)}`"
                for i, t in enumerate(tracks[start : start + 10])
            ]
            title = (
                f"**Your Favorites** ({total}/{self.bot.cfg.max_favorites_per_user})"
                if self.bot.cfg.max_favorites_per_user > 0
                else f"**Your Favorites** ({total})"
            )
            return PaginatedPage(
                title=title,
                items=items,
                current_page=page,
                total_pages=pages,
                empty_message=messages.favorites_empty(),
            )

        view = PaginatedView(
            items_provider=favorites_provider,
            author_id=user_id,
            guild_id=interaction.guild_id or 0,
            kind="favorites",
        )
        await view.render(1)
        await reply_card(interaction, view, ephemeral=True)

    @favorites_group.command(name="add", description="Add the currently playing track to your favorites")
    @app_commands.guild_only()
    async def favorites_add(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id or 0
        player = self.bot.registry.get(guild_id)
        if player is None or player.current is None:
            raise NothingPlaying()

        current = player.current
        if not await safe_defer(interaction, ephemeral=True):
            return

        try:
            existing = await self.bot.storage.get_favorites(interaction.user.id)
            if self.bot.cfg.max_favorites_per_user > 0 and len(existing) >= self.bot.cfg.max_favorites_per_user:
                raise BotUserError(messages.favorites_limit(self.bot.cfg.max_favorites_per_user))

            stored = StoredTrack(
                uri=current.uri,
                title=current.title,
                artist=current.artist,
                duration_ms=current.duration_ms,
                requester_id=interaction.user.id,
            )
            await self.bot.storage.add_favorite(interaction.user.id, stored, limit=self.bot.cfg.max_favorites_per_user)
            self._fav_cache._data.pop(interaction.user.id, None)
            await reply(interaction, messages.favorite_added(truncate(current.title)), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @favorites_group.command(name="play", description="Queue and play your favorite tracks")
    @app_commands.describe(shuffle="Shuffle favorites before queuing")
    @app_commands.guild_only()
    async def favorites_play(self, interaction: discord.Interaction, shuffle: bool = False) -> None:
        member, channel = await self._resolve_voice(interaction)
        if not await safe_defer(interaction):
            return

        try:
            tracks = await self.bot.storage.get_favorites(interaction.user.id)
        except StorageError:
            await reply(interaction, messages.storage_unavailable())
            return

        if not tracks:
            raise BotUserError(messages.favorites_empty())

        if shuffle:
            tracks = list(tracks)
            random.shuffle(tracks)

        avatar_url = extract_user_avatar_url(member)
        items: list[QueueItem] = []
        for t in tracks:
            # If track is a Spotify URI, create as Spotify item for lazy resolution
            if parse_spotify_url(t.uri):
                items.append(QueueItem.from_spotify(t, member.id, requester_avatar_url=avatar_url))
            else:
                items.append(
                    QueueItem(
                        track=None,
                        title=t.title,
                        duration_ms=t.duration_ms,
                        requester_id=member.id,
                        artist=t.artist,
                        uri=t.uri,
                        query=t.uri,
                        requester_avatar_url=avatar_url,
                    )
                )

        player = await self.bot.registry.get_or_create(interaction.guild_id or 0, channel.id, interaction.channel_id or 0)
        res = await player.enqueue(items)
        await reply(interaction, messages.favorites_queued(res.added, res.skipped))

    @favorites_group.command(name="remove", description="Remove a track from your favorites by position")
    @app_commands.describe(position="Position number from /favorites list")
    @app_commands.guild_only()
    async def favorites_remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 1000]) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        try:
            removed = await self.bot.storage.remove_favorite(interaction.user.id, position)
            self._fav_cache._data.pop(interaction.user.id, None)
            if removed:
                await reply(interaction, messages.favorite_position_removed(position), ephemeral=True)
            else:
                await reply(interaction, messages.favorite_not_found(position), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @favorites_remove.autocomplete("position")
    async def favorites_remove_autocomplete(
        self, interaction: discord.Interaction, current: int | str
    ) -> list[app_commands.Choice[int]]:
        try:
            if not interaction.guild or not interaction.guild_id:
                return []
            user_id = interaction.user.id if interaction.user else 0
            if not user_id:
                return []
            tracks = await self._get_user_favorites(user_id)
            if not tracks:
                return []
            typed = str(current).strip().lower()
            matches: list[app_commands.Choice[int]] = []
            for idx, track in enumerate(tracks, start=1):
                idx_str = str(idx)
                if typed:
                    num_match = idx_str.startswith(typed)
                    title_match = typed in (track.title or "").lower()
                    artist_match = bool(track.artist and typed in track.artist.lower())
                    if not (num_match or title_match or artist_match):
                        continue
                dur = format_duration(track.duration_ms) if track.duration_ms else "0:00"
                if track.artist:
                    label = f"{idx}. {track.title} - {track.artist} ({dur})"
                else:
                    label = f"{idx}. {track.title} ({dur})"
                label = clean(label)[:100]
                matches.append(app_commands.Choice(name=label, value=idx))
                if len(matches) >= 25:
                    break
            return matches
        except Exception:
            return []

    @favorites_group.command(name="clear", description="Clear all your favorite tracks")
    @app_commands.guild_only()
    async def favorites_clear(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        try:
            await self.bot.storage.clear_favorites(interaction.user.id)
            self._fav_cache._data.pop(interaction.user.id, None)
            await reply(interaction, messages.favorites_cleared(), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    # ------------------------------------------------------------- playlists
    playlist_group = app_commands.Group(name="playlist", description="Manage personal playlists")

    @playlist_group.command(name="create", description="Create a new playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.guild_only()
    async def playlist_create(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 50]) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        clean_name = name.strip()
        try:
            existing = await self.bot.storage.list_playlists(interaction.user.id)
            if self.bot.cfg.max_playlists_per_user > 0 and len(existing) >= self.bot.cfg.max_playlists_per_user:
                raise BotUserError(messages.playlist_limit(self.bot.cfg.max_playlists_per_user))

            await self.bot.storage.create_playlist(
                interaction.user.id, clean_name, limit=self.bot.cfg.max_playlists_per_user
            )
            self._playlists_cache._data.pop(interaction.user.id, None)
            await reply(interaction, messages.playlist_created(clean_name), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @playlist_group.command(name="delete", description="Delete a playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.guild_only()
    async def playlist_delete(self, interaction: discord.Interaction, name: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        clean_name = name.strip()
        try:
            deleted = await self.bot.storage.delete_playlist(interaction.user.id, clean_name)
            self._playlists_cache._data.pop(interaction.user.id, None)
            self._tracks_cache._data.pop((interaction.user.id, clean_name.lower()), None)
            if deleted:
                await reply(interaction, messages.playlist_deleted(clean_name), ephemeral=True)
            else:
                await reply(interaction, messages.playlist_not_found(clean_name), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @playlist_delete.autocomplete("name")
    async def playlist_delete_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._playlist_name_autocomplete(interaction, current)

    @playlist_group.command(name="rename", description="Rename an existing playlist")
    @app_commands.describe(old_name="Current playlist name", new_name="New playlist name")
    @app_commands.guild_only()
    async def playlist_rename(
        self,
        interaction: discord.Interaction,
        old_name: str,
        new_name: app_commands.Range[str, 1, 50],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        clean_old = old_name.strip()
        clean_new = new_name.strip()
        try:
            renamed = await self.bot.storage.rename_playlist(interaction.user.id, clean_old, clean_new)
            self._playlists_cache._data.pop(interaction.user.id, None)
            self._tracks_cache._data.pop((interaction.user.id, clean_old.lower()), None)
            self._tracks_cache._data.pop((interaction.user.id, clean_new.lower()), None)
            if renamed:
                await reply(interaction, messages.playlist_renamed(clean_old, clean_new), ephemeral=True)
            else:
                await reply(interaction, messages.playlist_not_found(clean_old), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @playlist_rename.autocomplete("old_name")
    async def playlist_rename_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._playlist_name_autocomplete(interaction, current)

    @playlist_group.command(name="list", description="List all your playlists")
    @app_commands.guild_only()
    async def playlist_list(self, interaction: discord.Interaction) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        user_id = interaction.user.id

        async def playlists_provider(page_num: int) -> PaginatedPage:
            try:
                names = await self.bot.storage.list_playlists(user_id)
            except StorageError:
                return PaginatedPage(
                    title="**Your Playlists**",
                    items=[],
                    current_page=1,
                    total_pages=1,
                    empty_message="Storage is temporarily unavailable.",
                )
            total = len(names)
            pages = max(1, math.ceil(total / 10))
            page = max(1, min(page_num, pages))
            start = (page - 1) * 10
            items = [f"{start + i + 1}. **{messages.escape_subject(n)}**" for i, n in enumerate(names[start : start + 10])]
            title = (
                f"**Your Playlists** ({total}/{self.bot.cfg.max_playlists_per_user})"
                if self.bot.cfg.max_playlists_per_user > 0
                else f"**Your Playlists** ({total})"
            )
            return PaginatedPage(
                title=title,
                items=items,
                current_page=page,
                total_pages=pages,
                empty_message=messages.playlists_empty_list(),
            )

        view = PaginatedView(
            items_provider=playlists_provider,
            author_id=user_id,
            guild_id=interaction.guild_id or 0,
            kind="playlists",
        )
        await view.render(1)
        await reply_card(interaction, view, ephemeral=True)

    @playlist_group.command(name="view", description="View tracks in a playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.guild_only()
    async def playlist_view(self, interaction: discord.Interaction, name: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        user_id = interaction.user.id
        clean_name = name.strip()

        async def playlist_view_provider(page_num: int) -> PaginatedPage:
            try:
                tracks = await self.bot.storage.get_playlist_tracks(user_id, clean_name)
            except StorageError:
                return PaginatedPage(
                    title=f"**Playlist: {messages.escape_subject(clean_name)}**",
                    items=[],
                    current_page=1,
                    total_pages=1,
                    empty_message=messages.storage_unavailable(),
                )
            total = len(tracks)
            pages = max(1, math.ceil(total / 10))
            page = max(1, min(page_num, pages))
            start = (page - 1) * 10
            items = [
                f"{start + i + 1}. **{messages.escape_subject(t.title)}** • `{format_duration(t.duration_ms)}`"
                for i, t in enumerate(tracks[start : start + 10])
            ]
            return PaginatedPage(
                title=f"**Playlist: {messages.escape_subject(clean_name)}** ({total} tracks)",
                items=items,
                current_page=page,
                total_pages=pages,
                empty_message=messages.playlist_empty_or_missing(clean_name),
            )

        view = PaginatedView(
            items_provider=playlist_view_provider,
            author_id=user_id,
            guild_id=interaction.guild_id or 0,
            kind="playlist_view",
        )
        await view.render(1)
        await reply_card(interaction, view, ephemeral=True)

    @playlist_view.autocomplete("name")
    async def playlist_view_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._playlist_name_autocomplete(interaction, current)

    @playlist_group.command(name="add", description="Add a track or current song to a playlist")
    @app_commands.describe(name="Playlist name", query="Track URL or search text (leave empty for current playing song)")
    @app_commands.guild_only()
    async def playlist_add(
        self,
        interaction: discord.Interaction,
        name: str,
        query: str | None = None,
    ) -> None:
        clean_name = name.strip()
        if not await safe_defer(interaction, ephemeral=True):
            return

        try:
            existing = await self.bot.storage.get_playlist_tracks(interaction.user.id, clean_name)
            if self.bot.cfg.max_tracks_per_playlist > 0 and len(existing) >= self.bot.cfg.max_tracks_per_playlist:
                raise BotUserError(messages.playlist_tracks_limit(self.bot.cfg.max_tracks_per_playlist))

            if query is None:
                guild_id = interaction.guild_id or 0
                player = self.bot.registry.get(guild_id)
                if player is None or player.current is None:
                    raise NothingPlaying(messages.nothing_playing())
                current = player.current
                stored = StoredTrack(
                    uri=current.uri,
                    title=current.title,
                    artist=current.artist,
                    duration_ms=current.duration_ms,
                    requester_id=interaction.user.id,
                )
            else:
                sp_info = parse_spotify_url(query)
                if sp_info is not None and sp_info[0] == "track":
                    sp_track = await self.bot.spotify.get_track(sp_info[1])
                    if sp_track is None:
                        raise BotUserError(messages.spotify_track_not_found())
                    stored = StoredTrack(
                        uri=sp_track.uri,
                        title=sp_track.title,
                        artist=sp_track.artist_name,
                        duration_ms=sp_track.duration_ms,
                        requester_id=interaction.user.id,
                    )
                else:
                    outcome = await self.bot.loader.load(interaction.guild_id or 0, query)
                    t = outcome.tracks[0]
                    stored = StoredTrack(
                        uri=str(getattr(t, "uri", "") or getattr(t, "identifier", "")),
                        title=str(getattr(t, "title", "Unknown")),
                        artist=str(getattr(t, "author", "")),
                        duration_ms=int(getattr(t, "duration", 0) or 0),
                        requester_id=interaction.user.id,
                    )

            await self.bot.storage.add_playlist_track(
                interaction.user.id, clean_name, stored, limit=self.bot.cfg.max_tracks_per_playlist
            )
            self._tracks_cache._data.pop((interaction.user.id, clean_name.lower()), None)
            await reply(interaction, messages.playlist_track_added(clean_name, truncate(stored.title)), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @playlist_add.autocomplete("name")
    async def playlist_add_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._playlist_name_autocomplete(interaction, current)

    @playlist_group.command(name="remove", description="Remove a track from a playlist by index")
    @app_commands.describe(name="Playlist name", index="Track index from /playlist view")
    @app_commands.guild_only()
    async def playlist_remove(
        self,
        interaction: discord.Interaction,
        name: str,
        index: app_commands.Range[int, 1, 1000],
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        clean_name = name.strip()
        try:
            removed = await self.bot.storage.remove_playlist_track(interaction.user.id, clean_name, index)
            self._tracks_cache._data.pop((interaction.user.id, clean_name.lower()), None)
            if removed:
                await reply(interaction, messages.playlist_track_removed_index(clean_name, index), ephemeral=True)
            else:
                await reply(interaction, messages.playlist_index_not_found(clean_name, index), ephemeral=True)
        except StorageError:
            await reply(interaction, messages.storage_unavailable(), ephemeral=True)

    @playlist_remove.autocomplete("name")
    async def playlist_remove_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._playlist_name_autocomplete(interaction, current)

    @playlist_remove.autocomplete("index")
    async def playlist_remove_index_autocomplete(
        self, interaction: discord.Interaction, current: int | str
    ) -> list[app_commands.Choice[int]]:
        try:
            if not interaction.guild or not interaction.guild_id:
                return []
            user_id = interaction.user.id if interaction.user else 0
            if not user_id:
                return []
            playlist_name = getattr(interaction.namespace, "name", None)
            if not playlist_name or not str(playlist_name).strip():
                return []
            tracks = await self._get_user_playlist_tracks(user_id, str(playlist_name).strip())
            if not tracks:
                return []
            typed = str(current).strip().lower()
            matches: list[app_commands.Choice[int]] = []
            for idx, track in enumerate(tracks, start=1):
                idx_str = str(idx)
                if typed:
                    num_match = idx_str.startswith(typed)
                    title_match = typed in (track.title or "").lower()
                    artist_match = bool(track.artist and typed in track.artist.lower())
                    if not (num_match or title_match or artist_match):
                        continue
                dur = format_duration(track.duration_ms) if track.duration_ms else "0:00"
                if track.artist:
                    label = f"{idx}. {track.title} - {track.artist} ({dur})"
                else:
                    label = f"{idx}. {track.title} ({dur})"
                label = clean(label)[:100]
                matches.append(app_commands.Choice(name=label, value=idx))
                if len(matches) >= 25:
                    break
            return matches
        except Exception:
            return []

    @playlist_group.command(name="play", description="Queue and play an entire playlist")
    @app_commands.describe(name="Playlist name", shuffle="Shuffle playlist before queuing")
    @app_commands.guild_only()
    async def playlist_play(self, interaction: discord.Interaction, name: str, shuffle: bool = False) -> None:
        member, channel = await self._resolve_voice(interaction)
        clean_name = name.strip()
        if not await safe_defer(interaction):
            return

        try:
            tracks = await self.bot.storage.get_playlist_tracks(interaction.user.id, clean_name)
        except StorageError:
            await reply(interaction, messages.storage_unavailable())
            return

        if not tracks:
            raise BotUserError(messages.playlist_empty_or_missing(clean_name))

        if shuffle:
            tracks = list(tracks)
            random.shuffle(tracks)

        avatar_url = extract_user_avatar_url(member)
        items: list[QueueItem] = []
        for t in tracks:
            if parse_spotify_url(t.uri):
                items.append(QueueItem.from_spotify(t, member.id, requester_avatar_url=avatar_url))
            else:
                items.append(
                    QueueItem(
                        track=None,
                        title=t.title,
                        duration_ms=t.duration_ms,
                        requester_id=member.id,
                        artist=t.artist,
                        uri=t.uri,
                        query=t.uri,
                        requester_avatar_url=avatar_url,
                    )
                )

        player = await self.bot.registry.get_or_create(interaction.guild_id or 0, channel.id, interaction.channel_id or 0)
        res = await player.enqueue(items)
        await reply(interaction, messages.playlist_queued(clean_name, res.added, res.skipped))

    @playlist_play.autocomplete("name")
    async def playlist_play_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._playlist_name_autocomplete(interaction, current)


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Library(bot))
