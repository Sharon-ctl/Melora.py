"""Music slash commands. All commands are guild-only."""
from __future__ import annotations

import asyncio
import logging
import math
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from core.guild_player import GuildPlayer
from core.lavalink_service import URL_RE, LoadOutcome
from core.queue import LoopMode, QueueItem, extract_user_avatar_url, validate_track
from core.spotify import parse_spotify_url
from core.storage import StorageError, StoredTrack
from utils.cache import CooldownTracker, TTLCache
from utils.checks import check_bot_channel_permissions, guild_member, is_privileged, require_dj, user_voice_channel
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
from utils.errors import (
    BotUserError,
    LoadFailed,
    NoMatches,
    NotInVoice,
    NothingPlaying,
    StageUnsupported,
    TrackTooLong,
    WrongChannel,
)
from utils import messages
from utils.interaction import reply, safe_defer
from utils.text import clean, format_duration, parse_time_string, truncate

if TYPE_CHECKING:
    from main import MusicBot

log = logging.getLogger(__name__)

QUEUE_PAGE_SIZE = 10
AUTOCOMPLETE_TIMEOUT = 2.0


class SearchView(BaseCardView):
    """Components V2 search selection card with up to 5 buttons."""

    def __init__(
        self,
        requester_id: int,
        tracks: list[Any],
        cog: Music,
        guild_id: int,
        voice_channel_id: int,
        text_channel_id: int,
    ) -> None:
        super().__init__(timeout=60.0, author_id=requester_id, guild_id=guild_id, kind="search")
        self.tracks = tracks
        self.cog = cog
        self.guild_id = guild_id
        self.voice_channel_id = voice_channel_id
        self.text_channel_id = text_channel_id
        self.picked = False

        lines = ["### Search Results", ""]
        for idx, t in enumerate(tracks, 1):
            dur = format_duration(int(getattr(t, "duration", 0) or 0))
            lines.append(f"**{idx}.** {truncate(getattr(t, 'title', 'Unknown'), 50)} [{dur}]")
        lines.append("")
        lines.append("Click a button below to choose a track.")

        text_disp = TextDisplay("\n".join(lines))
        buttons: list[Button[Any]] = []
        for idx in range(1, len(tracks) + 1):
            btn = secondary_button(str(idx))
            btn.callback = self._make_callback(idx - 1)
            buttons.append(btn)

        row = ActionRow(*buttons)
        container = create_card_container(text_disp, row)
        self.add_item(container)

    def release_references(self) -> None:
        super().release_references()
        self.cog = None  # type: ignore
        self.tracks = []

    def _make_callback(self, track_idx: int) -> Any:
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.author_id:
                await interaction.response.send_message(messages.not_your_menu(), ephemeral=True)
                return
            if self.picked:
                await interaction.response.send_message(messages.track_already_chosen(), ephemeral=True)
                return
            self.picked = True
            chosen_track = self.tracks[track_idx] if track_idx < len(self.tracks) else None
            cog = self.cog
            if not interaction.response.is_done():
                await interaction.response.defer()
            await self.disable_and_stop()

            if chosen_track is not None and cog is not None:
                player = await cog.bot.registry.get_or_create(self.guild_id, self.voice_channel_id, self.text_channel_id)
                item = QueueItem.from_track(
                    chosen_track, self.author_id or 0, requester_avatar_url=extract_user_avatar_url(interaction.user)
                )
                await player.enqueue([item])
                await interaction.followup.send(messages.single_track_added(truncate(item.title)))

        return callback


class Music(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        self._search_cache: TTLCache[str, list[app_commands.Choice[str]]] = TTLCache(max_size=1000, ttl=300.0)
        self._search_rate_limit = CooldownTracker(1.0, max_size=2048)

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

    # ------------------------------------------------------------------ helpers

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

    async def _load_query_items(
        self, query: str, user_id: int, guild_id: int, requester_avatar_url: str | None = None
    ) -> tuple[list[QueueItem], int, str | None, int | None]:
        """Resolve a query into queue items, supporting Spotify URLs and standard search.

        Returns (items, skipped_count, collection_name, total_in_collection).
        """
        spotify_info = parse_spotify_url(query)
        if spotify_info is not None:
            if not self.bot.cfg.spotify_enabled:
                raise BotUserError(messages.spotify_disabled())
            kind, spotify_id = spotify_info
            cfg = self.bot.cfg
            if kind == "track":
                track = await self.bot.spotify.get_track(spotify_id)
                if track is None:
                    raise NoMatches(messages.spotify_track_not_found())
                if track.duration_ms and track.duration_ms // 1000 > cfg.max_track_seconds:
                    raise TrackTooLong()
                return [QueueItem.from_spotify(track, user_id, requester_avatar_url=requester_avatar_url)], 0, None, None

            if kind == "album":
                album = await self.bot.spotify.get_album(spotify_id)
                if album is None or not album.tracks:
                    raise NoMatches(messages.spotify_album_not_found())
                chosen = album.tracks[: cfg.max_playlist_tracks]
                skipped = album.total_tracks - len(chosen)
                items: list[QueueItem] = []
                for t in chosen:
                    if t.duration_ms and t.duration_ms // 1000 > cfg.max_track_seconds:
                        skipped += 1
                        continue
                    items.append(QueueItem.from_spotify(t, user_id, requester_avatar_url=requester_avatar_url))
                if not items:
                    raise TrackTooLong(messages.collection_all_tracks_too_long("album"))
                return items, skipped, album.title, album.total_tracks

            if kind == "playlist":
                playlist = await self.bot.spotify.get_playlist(spotify_id)
                if playlist is None or not playlist.tracks:
                    raise NoMatches(messages.spotify_playlist_not_found())
                chosen = playlist.tracks[: cfg.max_playlist_tracks]
                skipped = playlist.total_tracks - len(chosen)
                items = []
                for t in chosen:
                    if t.duration_ms and t.duration_ms // 1000 > cfg.max_track_seconds:
                        skipped += 1
                        continue
                    items.append(QueueItem.from_spotify(t, user_id, requester_avatar_url=requester_avatar_url))
                if not items:
                    raise TrackTooLong(messages.collection_all_tracks_too_long("playlist"))
                return items, skipped, playlist.title, playlist.total_tracks

        outcome = await self.bot.loader.load(guild_id, query)
        items, skipped = self._build_items(outcome, user_id, requester_avatar_url=requester_avatar_url)
        collection_name = outcome.playlist_name if outcome.kind == "playlist" else None
        total_tracks = len(outcome.tracks) if outcome.kind == "playlist" else None
        return items, skipped, collection_name, total_tracks

    def _build_items(
        self, outcome: LoadOutcome, user_id: int, requester_avatar_url: str | None = None
    ) -> tuple[list[QueueItem], int]:
        """Turn a load result into queue items. Returns (items, skipped_count)."""
        cfg = self.bot.cfg
        if outcome.kind != "playlist":
            track = outcome.tracks[0]
            validate_track(track, cfg.max_track_seconds)
            item = QueueItem.from_track(
                track,
                user_id,
                query=outcome.query,
                fallback_used=outcome.used_fallback,
                requester_avatar_url=requester_avatar_url,
            )
            return [item], 0
        chosen = outcome.tracks[: cfg.max_playlist_tracks]
        skipped = len(outcome.tracks) - len(chosen)
        items: list[QueueItem] = []
        for track in chosen:
            try:
                validate_track(track, cfg.max_track_seconds)
            except TrackTooLong:
                skipped += 1
                continue
            items.append(QueueItem.from_track(track, user_id, requester_avatar_url=requester_avatar_url))
        if not items:
            raise TrackTooLong(messages.collection_all_tracks_too_long("playlist"))
        return items, skipped

    async def _resolve_voice_channel(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Member, discord.VoiceChannel | discord.StageChannel]:
        member = guild_member(interaction)
        guild = interaction.guild
        assert guild is not None
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
        return member, channel

    # --------------------------------------------------------------------- play

    @app_commands.command(name="play", description="Play a link or search for a song")
    @app_commands.describe(query="A link or search text")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: app_commands.Range[str, 1, 200]) -> None:
        member, channel = await self._resolve_voice_channel(interaction)
        if not await safe_defer(interaction):
            return

        guild_id = interaction.guild_id or 0
        avatar_url = extract_user_avatar_url(member)
        items, skipped, coll_name, coll_total = await self._load_query_items(
            query, member.id, guild_id, requester_avatar_url=avatar_url
        )

        voice = member.voice
        if voice is None or voice.channel is None or voice.channel.id != channel.id:
            raise NotInVoice()
        player = await self.bot.registry.get_or_create(guild_id, channel.id, interaction.channel_id or 0)
        if player.voice_channel_id != channel.id:
            raise WrongChannel()
        result = await player.enqueue(items)
        if not result.started and player.current is None and len(player.queue) == 0:
            raise LoadFailed(messages.playback_start_failed())

        total_skipped = skipped + result.skipped
        if coll_name is not None:
            total_reported = coll_total or (result.added + total_skipped)
            text = messages.collection_tracks_added(truncate(coll_name, 50), result.added, total_reported, total_skipped)
        else:
            dur_str = format_duration(items[0].duration_ms) if items and items[0].duration_ms else None
            text = messages.single_track_added(items[0].title, result.position, duration_str=dur_str)
        await reply(interaction, text)

    @app_commands.command(name="insert", description="Insert a song at a given position in the queue")
    @app_commands.describe(query="A link or search text", position="1-based position in queue (default 1)")
    @app_commands.guild_only()
    async def insert(
        self,
        interaction: discord.Interaction,
        query: app_commands.Range[str, 1, 200],
        position: app_commands.Range[int, 1, 100000] = 1,
    ) -> None:
        member, channel = await self._resolve_voice_channel(interaction)
        if not await safe_defer(interaction):
            return

        guild_id = interaction.guild_id or 0
        avatar_url = extract_user_avatar_url(member)
        items, _, _, _ = await self._load_query_items(query, member.id, guild_id, requester_avatar_url=avatar_url)
        player = await self.bot.registry.get_or_create(guild_id, channel.id, interaction.channel_id or 0)
        pos = await player.insert(items[0], position)
        if pos == 0:
            await reply(interaction, messages.now_playing(truncate(items[0].title)))
        else:
            await reply(interaction, messages.track_inserted(truncate(items[0].title), pos))

    @app_commands.command(name="playnext", description="Queue a track to play next")
    @app_commands.describe(query="A link or search text")
    @app_commands.guild_only()
    async def playnext(self, interaction: discord.Interaction, query: app_commands.Range[str, 1, 200]) -> None:
        member, channel = await self._resolve_voice_channel(interaction)
        if not await safe_defer(interaction):
            return

        guild_id = interaction.guild_id or 0
        avatar_url = extract_user_avatar_url(member)
        items, _, _, _ = await self._load_query_items(query, member.id, guild_id, requester_avatar_url=avatar_url)
        player = await self.bot.registry.get_or_create(guild_id, channel.id, interaction.channel_id or 0)
        pos = await player.insert(items[0], 1)
        if pos == 0:
            await reply(interaction, messages.now_playing(truncate(items[0].title)))
        else:
            await reply(interaction, messages.playing_next(truncate(items[0].title), None, None))

    @app_commands.command(name="playinstant", description="Start a track immediately without altering the queue")
    @app_commands.describe(query="A link or search text")
    @app_commands.guild_only()
    async def playinstant(self, interaction: discord.Interaction, query: app_commands.Range[str, 1, 200]) -> None:
        member, channel = await self._resolve_voice_channel(interaction)
        if not await safe_defer(interaction):
            return

        guild_id = interaction.guild_id or 0
        avatar_url = extract_user_avatar_url(member)
        items, _, _, _ = await self._load_query_items(query, member.id, guild_id, requester_avatar_url=avatar_url)
        player = await self.bot.registry.get_or_create(guild_id, channel.id, interaction.channel_id or 0)
        await player.play_instant(items[0])
        await reply(interaction, messages.now_playing(truncate(items[0].title)))

    async def _search_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            if not interaction.guild or not interaction.guild_id:
                return []
            if not getattr(self.bot.cfg, "autocomplete_search_enabled", True):
                return []
            loader = getattr(self.bot, "loader", None)
            if loader is None or not loader.has_node():
                return []
            query = current.strip()
            if len(query) < 3 or URL_RE.match(query):
                return []
            key = query.lower()
            cached = self._search_cache.get(key)
            if cached is not None:
                return cached
            user_id = interaction.user.id if interaction.user else 0
            if user_id and self._search_rate_limit.hit(user_id) > 0:
                return []
            candidates = await asyncio.wait_for(loader.search_candidates(query, limit=25), timeout=2.0)
            choices: list[app_commands.Choice[str]] = []
            for title, author in candidates:
                if author:
                    label = f"{title} - {author}"
                else:
                    label = title
                label = clean(label)[:100]
                choices.append(app_commands.Choice(name=label, value=label))
                if len(choices) >= 25:
                    break
            self._search_cache.set(key, choices)
            return choices
        except Exception:
            return []

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

    @play.autocomplete("query")
    @insert.autocomplete("query")
    @playnext.autocomplete("query")
    @playinstant.autocomplete("query")
    async def play_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._search_autocomplete(interaction, current)

    # ---------------------------------------------------------- playback control

    @app_commands.command(name="pause", description="Pause playback")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        player = self._control_gate(interaction)
        if not await safe_defer(interaction):
            return
        await player.pause()
        await reply(interaction, messages.paused())

    @app_commands.command(name="resume", description="Resume playback")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        player = self._control_gate(interaction)
        if not await safe_defer(interaction):
            return
        await player.resume()
        await reply(interaction, messages.resumed())

    @app_commands.command(name="skip", description="Skip the current track")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        player = self._control_gate(interaction)
        member = guild_member(interaction)
        guild = interaction.guild
        assert guild is not None

        humans = self.bot.backend.humans_in_channel(guild.id, player.voice_channel_id) or 1
        is_requester = player.current is not None and player.current.requester_id == member.id
        privileged = is_privileged(self.bot, member)

        has_dj = False
        if self.bot.cfg.dj_role_id:
            has_dj = any(r.id == self.bot.cfg.dj_role_id for r in member.roles)

        # Immediate skip if requester, privileged, DJ, voting disabled, or < min listeners
        if (
            is_requester
            or privileged
            or has_dj
            or not self.bot.cfg.vote_skip_enabled
            or humans < self.bot.cfg.vote_skip_min_listeners
        ):
            if not await safe_defer(interaction):
                return
            item = await player.skip()
            await reply(interaction, messages.skipped(truncate(item.title)))
            return

        # Otherwise vote skip
        if not await safe_defer(interaction):
            return
        skipped, votes, needed = await player.vote_skip(member.id, humans)
        if skipped:
            await reply(interaction, messages.vote_skip_passed())
        else:
            await reply(interaction, messages.vote_skip_registered(votes, needed))

    @app_commands.command(name="previous", description="Play the previous track from history")
    @app_commands.guild_only()
    async def previous(self, interaction: discord.Interaction) -> None:
        player = self._control_gate(interaction)
        if not await safe_defer(interaction):
            return
        item = await player.previous()
        await reply(interaction, messages.previous_track(truncate(item.title)))

    @app_commands.command(name="replay", description="Replay the current track from the beginning")
    @app_commands.guild_only()
    async def replay(self, interaction: discord.Interaction) -> None:
        player = self._control_gate(interaction)
        if not await safe_defer(interaction):
            return
        await player.replay()
        await reply(interaction, messages.replayed(truncate(player.current.title) if player.current else "current track"))

    @app_commands.command(name="seek", description="Seek to a position in the current track")
    @app_commands.describe(position="Position (e.g. 1:30 or 90)")
    @app_commands.guild_only()
    async def seek(self, interaction: discord.Interaction, position: app_commands.Range[str, 1, 20]) -> None:
        player = self._control_gate(interaction)
        seconds = parse_time_string(position)
        if seconds is None:
            raise BotUserError(messages.invalid_time_format())
        if not await safe_defer(interaction):
            return
        await player.seek(seconds)
        await reply(interaction, messages.seeked(format_duration(seconds * 1000)))

    @app_commands.command(name="forward", description="Jump forward by seconds")
    @app_commands.describe(seconds="Number of seconds to skip forward")
    @app_commands.guild_only()
    async def forward(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 1, 86400]) -> None:
        player = self._control_gate(interaction)
        if not await safe_defer(interaction):
            return
        new_pos = await player.forward(seconds)
        await reply(interaction, messages.forwarded(seconds, format_duration(new_pos * 1000)))

    @app_commands.command(name="rewind", description="Jump backward by seconds")
    @app_commands.describe(seconds="Number of seconds to skip backward")
    @app_commands.guild_only()
    async def rewind(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 1, 86400]) -> None:
        player = self._control_gate(interaction)
        if not await safe_defer(interaction):
            return
        new_pos = await player.rewind(seconds)
        await reply(interaction, messages.rewound(seconds, format_duration(new_pos * 1000)))

    @app_commands.command(name="skipto", description="Skip directly to a position in the queue")
    @app_commands.describe(position="Position shown in /queue")
    @app_commands.guild_only()
    async def skipto(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 100000]) -> None:
        player = self._control_gate(interaction)
        if not await safe_defer(interaction):
            return
        item, dropped = await player.skipto(position)
        await reply(interaction, messages.skipto_result(position, truncate(item.title), len(dropped)))

    @skipto.autocomplete("position")
    async def skipto_autocomplete(
        self, interaction: discord.Interaction, current: int | str
    ) -> list[app_commands.Choice[int]]:
        return await self._queue_pos_autocomplete(interaction, current)

    @app_commands.command(name="sleep", description="Stop playback and leave after N minutes (0 to cancel)")
    @app_commands.describe(minutes="Minutes until leaving (0 cancels)")
    @app_commands.guild_only()
    async def sleep(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 0, 1440]) -> None:
        player = self._control_gate(interaction)
        player.set_sleep(minutes)
        if minutes == 0:
            await reply(interaction, messages.sleep_cancelled())
        else:
            await reply(interaction, messages.sleep_set(minutes))

    @app_commands.command(name="search", description="Search for a track and choose from the top 5 results")
    @app_commands.describe(query="Search text")
    @app_commands.guild_only()
    async def search(self, interaction: discord.Interaction, query: app_commands.Range[str, 1, 200]) -> None:
        member, channel = await self._resolve_voice_channel(interaction)
        if not await safe_defer(interaction):
            return

        outcome = await self.bot.loader.load(interaction.guild_id or 0, query)
        tracks = outcome.tracks[:5]
        if not tracks:
            raise NoMatches()

        view = SearchView(
            requester_id=member.id,
            tracks=tracks,
            cog=self,
            guild_id=interaction.guild_id or 0,
            voice_channel_id=channel.id,
            text_channel_id=interaction.channel_id or 0,
        )
        await reply_card(interaction, view)

    @search.autocomplete("query")
    async def search_query_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._search_autocomplete(interaction, current)

    @app_commands.command(name="autoplay", description="Toggle or view autoplay when the queue ends")
    @app_commands.describe(enabled="Turn autoplay on or off")
    @app_commands.guild_only()
    async def autoplay(self, interaction: discord.Interaction, enabled: bool | None = None) -> None:
        player = self._control_gate(interaction)
        guild_id = interaction.guild_id or 0
        new_state = (not player.autoplay) if enabled is None else enabled
        player.set_autoplay(new_state)

        storage = getattr(self.bot, "storage", None)
        if storage is not None:
            try:
                await storage.update_guild_settings(guild_id, autoplay=new_state)
            except StorageError as exc:
                log.debug("guild=%s could not persist autoplay setting: %s", guild_id, exc)

        await reply(interaction, messages.autoplay_toggled(new_state))

    @app_commands.command(name="similar", description="Queue up to 5 tracks similar to the current song")
    @app_commands.guild_only()
    async def similar(self, interaction: discord.Interaction) -> None:
        player = self._control_gate(interaction)
        if not await safe_defer(interaction):
            return
        items = await player.similar(count=5)
        if not items:
            await reply(interaction, messages.similar_not_found())
        else:
            await reply(interaction, messages.similar_added(len(items)))

    @app_commands.command(name="nowplaying", description="Show or move the now playing card to this channel")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        guild_member(interaction)
        player = self.bot.registry.get(interaction.guild_id or 0)
        if player is None or player.current is None:
            await reply(interaction, messages.nothing_playing(), ephemeral=True)
            return
        if not await safe_defer(interaction, ephemeral=True):
            return
        channel_id = interaction.channel_id or 0
        await player.move_card(channel_id)
        await reply(interaction, messages.nowplaying_moved(), ephemeral=True)

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        player = self._control_gate(interaction, dj=True)
        if not await safe_defer(interaction):
            return
        await player.stop()
        await reply(interaction, messages.stopped())

    @app_commands.command(name="leave", description="Leave the voice channel")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        self._control_gate(interaction)
        if not await safe_defer(interaction):
            return
        await self.bot.registry.destroy(interaction.guild_id or 0, "leave command")
        await reply(interaction, messages.left_channel())

    @app_commands.command(name="volume", description="Set the volume from 0 to 100")
    @app_commands.describe(level="Volume level")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]) -> None:
        player = self._control_gate(interaction, dj=True)
        if not await safe_defer(interaction):
            return
        storage = getattr(self.bot, "storage", None)
        limit = 100
        if storage is not None and interaction.guild_id:
            try:
                s = await storage.get_guild_settings(interaction.guild_id)
                limit = s.volume_limit
            except Exception as exc:
                log.debug("guild=%s failed checking volume limit: %s", interaction.guild_id, exc)
        target = min(level, limit)
        actual = await player.set_volume(target)
        if target < level:
            await reply(interaction, messages.volume_updated(actual, limit))
        else:
            await reply(interaction, messages.volume_updated(actual))

    # -------------------------------------------------------------- queue control

    @app_commands.command(name="queue", description="Show upcoming tracks")
    @app_commands.describe(page="Page number")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 1000] = 1) -> None:
        member = guild_member(interaction)
        player = self.bot.registry.get(interaction.guild_id or 0)
        if player is None:
            raise NothingPlaying()

        guild_id = interaction.guild_id or 0

        def queue_provider(page_num: int) -> PaginatedPage:
            p = self.bot.registry.get(guild_id)
            if p is None:
                return PaginatedPage(
                    title="**Music Queue**",
                    items=[],
                    current_page=1,
                    total_pages=1,
                    empty_message="Nothing is playing.",
                )
            current = p.current
            extra = None
            if current is not None:
                dur = format_duration(current.duration_ms)
                extra = f"Now: [{messages.escape_subject(current.title)}]({current.uri}) • `{dur}`\n"

            entries, p_num, total_p = p.queue.page(page_num, QUEUE_PAGE_SIZE)
            items = [
                f"{idx}. **{messages.escape_subject(item.title)}** • `{format_duration(item.duration_ms)}`"
                for idx, item in entries
            ]
            empty_msg = messages.queue_empty() if len(p.queue) == 0 else None
            return PaginatedPage(
                title="**Music Queue**",
                items=items,
                current_page=p_num,
                total_pages=total_p,
                extra_header=extra,
                empty_message=empty_msg,
            )

        view = PaginatedView(
            items_provider=queue_provider,
            author_id=member.id,
            guild_id=interaction.guild_id or 0,
            kind="queue",
            initial_page=page,
        )
        await view.render(page)
        await reply_card(interaction, view)

    @app_commands.command(name="remove", description="Remove track(s) from the queue")
    @app_commands.describe(position="Position shown in /queue", count="Number of tracks to remove (default 1)")
    @app_commands.guild_only()
    async def remove(
        self,
        interaction: discord.Interaction,
        position: app_commands.Range[int, 1, 100000],
        count: app_commands.Range[int, 1, 1000] = 1,
    ) -> None:
        player = self._control_gate(interaction)
        items = await player.remove_many(position, count)
        if len(items) == 1:
            await reply(interaction, messages.track_removed(truncate(items[0].title)))
        else:
            await reply(interaction, messages.track_removed(truncate(items[0].title), len(items)))

    @remove.autocomplete("position")
    async def remove_autocomplete(
        self, interaction: discord.Interaction, current: int | str
    ) -> list[app_commands.Choice[int]]:
        return await self._queue_pos_autocomplete(interaction, current)

    @app_commands.command(name="clear", description="Clear the queue")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction) -> None:
        player = self._control_gate(interaction, dj=True)
        count = await player.clear()
        await reply(interaction, messages.queue_cleared_count(count))

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction) -> None:
        player = self._control_gate(interaction)
        await player.shuffle()
        await reply(interaction, messages.shuffled(len(player.queue)))

    @app_commands.command(name="loop", description="Set the loop mode")
    @app_commands.describe(mode="off, track or queue")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="track", value="track"),
            app_commands.Choice(name="queue", value="queue"),
        ]
    )
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        player = self._control_gate(interaction)
        try:
            loop_mode = LoopMode(mode.value)
        except ValueError:
            raise BotUserError(messages.unknown_loop_mode()) from None
        player.set_loop(loop_mode)
        await reply(interaction, messages.loop_mode_set(loop_mode.value))

    @app_commands.command(name="move", description="Move a track to a new position in the queue")
    @app_commands.describe(from_position="Current position", to_position="New position")
    @app_commands.guild_only()
    async def move(
        self,
        interaction: discord.Interaction,
        from_position: app_commands.Range[int, 1, 100000],
        to_position: app_commands.Range[int, 1, 100000],
    ) -> None:
        player = self._control_gate(interaction)
        item = await player.move(from_position, to_position)
        await reply(interaction, messages.track_moved(truncate(item.title), from_position, to_position))

    @move.autocomplete("from_position")
    async def move_from_autocomplete(
        self, interaction: discord.Interaction, current: int | str
    ) -> list[app_commands.Choice[int]]:
        return await self._queue_pos_autocomplete(interaction, current)

    @move.autocomplete("to_position")
    async def move_to_autocomplete(
        self, interaction: discord.Interaction, current: int | str
    ) -> list[app_commands.Choice[int]]:
        return await self._queue_pos_autocomplete(interaction, current)

    @app_commands.command(name="swap", description="Swap two tracks in the queue")
    @app_commands.describe(position1="First position", position2="Second position")
    @app_commands.guild_only()
    async def swap(
        self,
        interaction: discord.Interaction,
        position1: app_commands.Range[int, 1, 100000],
        position2: app_commands.Range[int, 1, 100000],
    ) -> None:
        player = self._control_gate(interaction)
        item1, item2 = await player.swap(position1, position2)
        await reply(interaction, messages.track_swapped(truncate(item1.title), position1, truncate(item2.title), position2))

    @swap.autocomplete("position1")
    async def swap_p1_autocomplete(
        self, interaction: discord.Interaction, current: int | str
    ) -> list[app_commands.Choice[int]]:
        return await self._queue_pos_autocomplete(interaction, current)

    @swap.autocomplete("position2")
    async def swap_p2_autocomplete(
        self, interaction: discord.Interaction, current: int | str
    ) -> list[app_commands.Choice[int]]:
        return await self._queue_pos_autocomplete(interaction, current)

    @app_commands.command(name="dedupe", description="Remove duplicate tracks from the queue")
    @app_commands.guild_only()
    async def dedupe(self, interaction: discord.Interaction) -> None:
        player = self._control_gate(interaction)
        removed = await player.dedupe()
        await reply(interaction, messages.deduped(removed))

    @app_commands.command(name="history", description="Show recently played tracks")
    @app_commands.guild_only()
    async def history(self, interaction: discord.Interaction) -> None:
        member = guild_member(interaction)
        player = self.bot.registry.get(interaction.guild_id or 0)
        if player is None:
            raise NothingPlaying()

        guild_id = interaction.guild_id or 0

        def history_provider(page_num: int) -> PaginatedPage:
            p = self.bot.registry.get(guild_id)
            if p is None:
                return PaginatedPage(
                    title="**Playback History**",
                    items=[],
                    current_page=1,
                    total_pages=1,
                    empty_message="No history available.",
                )
            all_history = p.queue.get_history(50)
            total_items = len(all_history)
            total_pages = max(1, math.ceil(total_items / 10))
            p_num = max(1, min(page_num, total_pages))

            start = (p_num - 1) * 10
            slice_items = all_history[start : start + 10]
            items = [
                f"{start + i + 1}. **{messages.escape_subject(item.title)}** • `{format_duration(item.duration_ms)}`"
                for i, item in enumerate(slice_items)
            ]
            return PaginatedPage(
                title=f"**Playback History** ({total_items} tracks)",
                items=items,
                current_page=p_num,
                total_pages=total_pages,
                empty_message=messages.queue_empty(),
            )

        view = PaginatedView(
            items_provider=history_provider,
            author_id=member.id,
            guild_id=interaction.guild_id or 0,
            kind="history",
            initial_page=1,
        )
        await view.render(1)
        await reply_card(interaction, view)

    @app_commands.command(name="savequeue", description="Save the current queue as a playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.guild_only()
    async def savequeue(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 50]) -> None:
        member = guild_member(interaction)
        player = self.bot.registry.get(interaction.guild_id or 0)
        if player is None or (player.current is None and len(player.queue) == 0):
            raise NothingPlaying("Nothing is playing and the queue is empty.")

        storage = getattr(self.bot, "storage", None)
        if storage is None:
            await reply(interaction, messages.storage_unavailable())
            return

        all_items: list[QueueItem] = []
        if player.current is not None:
            all_items.append(player.current)
        all_items.extend(list(player.queue))

        clean_name = clean(name)
        try:
            try:
                await storage.create_playlist(member.id, clean_name, self.bot.cfg.max_playlists_per_user)
            except StorageError as exc:
                if "already exists" not in str(exc).lower():
                    raise

            added = 0
            for item in all_items:
                st = StoredTrack(uri=item.uri, title=item.title, artist=item.artist, duration_ms=item.duration_ms)
                try:
                    await storage.add_playlist_track(member.id, clean_name, st, self.bot.cfg.max_tracks_per_playlist)
                    added += 1
                except StorageError:
                    break

            await reply(interaction, messages.queue_saved(clean_name, added))
        except StorageError as exc:
            await reply(interaction, messages.queue_save_failed(str(exc)))


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Music(bot))
