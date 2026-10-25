"""Personal library utilities, Mewsic interchange, and karaoke."""

from __future__ import annotations

import io
import json
import logging
import random
import re
import time
import asyncio
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from core.queue import QueueItem, extract_user_avatar_url
from core.spotify import parse_spotify_url
from core.storage import StoredTrack
from utils.profile_canvas import render_profile, render_stats
from utils.checks import check_bot_channel_permissions, guild_member, require_dj, user_voice_channel
from utils.components_v2 import BaseCardView, TextDisplay, create_card_container, reply_card
from utils.errors import BotUserError, StageUnsupported, WrongChannel
from utils.interaction import reply, safe_defer
from utils.text import clean, format_duration, truncate

if TYPE_CHECKING:
    from main import MusicBot

log = logging.getLogger(__name__)
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_TRACKS = 250
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _mewsic(tracks: list[StoredTrack], name: str) -> bytes:
    rows = []
    ids = []
    for t in tracks:
        tid = f"{random.getrandbits(64):016x}"
        ids.append(tid)
        match = re.search(r"(?:youtu\.be/|youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})", t.uri)
        rows.append(
            {
                "id": tid,
                "title": t.title,
                "artist": t.artist,
                "album": "Unknown Album",
                "duration": max(0, t.duration_ms) / 1000,
                "file_path": "",
                "source_id": match.group(1) if match else None,
            }
        )
    return json.dumps(
        {
            "id": f"{random.getrandbits(64):016x}",
            "name": name,
            "file_path": "",
            "track_ids": ids,
            "tracks": rows,
            "created_at": int(time.time()),
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")


def _parse_mewsic(data: bytes) -> tuple[str, list[StoredTrack]]:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("That file is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tracks"), list):
        raise ValueError("Expected a Mewsic JSON file with a tracks array.")
    rows = payload["tracks"]
    if not rows or len(rows) > MAX_IMPORT_TRACKS:
        raise ValueError(f"The file must contain between 1 and {MAX_IMPORT_TRACKS} tracks.")
    out: list[StoredTrack] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = clean(str(row.get("title") or ""))[:200]
        artist = clean(str(row.get("artist") or ""))[:100]
        if not title:
            continue
        source_id = str(row.get("source_id") or "")
        uri = f"https://www.youtube.com/watch?v={source_id}" if YOUTUBE_ID.fullmatch(source_id) else ""
        try:
            duration = max(0, min(int(float(row.get("duration") or 0) * 1000), 86_400_000))
        except (TypeError, ValueError, OverflowError):
            duration = 0
        # Metadata without a validated source id is resolved by title/artist search.
        out.append(StoredTrack(uri=uri, title=title, artist=artist, duration_ms=duration))
    if not out:
        raise ValueError("No usable tracks were found in that file.")
    return clean(str(payload.get("name") or "Imported playlist"))[:80] or "Imported playlist", out


class Features(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        self._karaoke: dict[int, tuple[discord.Message, asyncio.Task[None]]] = {}

    @app_commands.command(name="dedicate", description="Add or clear a dedication on the now-playing card")
    @app_commands.describe(message="A short dedication (leave empty to clear it)")
    @app_commands.guild_only()
    async def dedicate(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 180] | None = None) -> None:
        member, _ = self._voice(interaction)
        player = self.bot.registry.get(interaction.guild_id or 0)
        if player is None or player.current is None:
            raise BotUserError("Nothing is playing right now.")
        if member.id != player.current.requester_id:
            require_dj(self.bot, self.bot.cfg, member, dj_role_id=player.dj_role_id, dj_only=player.dj_only)
        player.dedication = clean(message or "")[:180]
        player.schedule_card_update()
        response = "Dedication cleared." if not player.dedication else "Dedication added to the now-playing card."
        await reply(interaction, response, ephemeral=True)

    @app_commands.command(name="session-recap", description="Summarize the current player's recent session")
    @app_commands.guild_only()
    async def session_recap(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id or 0
        player = self.bot.registry.get(guild_id)
        if player is None:
            await reply(interaction, "There is no active music session in this server.", ephemeral=True)
            return
        recent = player.queue.get_history(50)
        tracks = list(recent)
        if player.current is not None:
            tracks.append(player.current)
        requesters: dict[int, int] = {}
        for item in tracks:
            if item.requester_id:
                requesters[item.requester_id] = requesters.get(item.requester_id, 0) + 1
        top = sorted(requesters.items(), key=lambda entry: (-entry[1], entry[0]))[:3]
        top_text = ", ".join(f"<@{uid}> ({count})" for uid, count in top) or "No requester data"
        estimated_ms = sum(max(0, item.duration_ms) for item in tracks if not item.is_stream)
        recent_titles = [f"- {truncate(item.title, 65)}" for item in tracks[-5:]]
        lines = [
            "### Listening Session Recap",
            f"**Player uptime:** `{format_duration(int(player.age() * 1000))}`",
            f"**Tracks represented:** `{len(tracks)}` (up to 50 recent completed plus current)",
            f"**Estimated track time:** `{format_duration(estimated_ms)}`",
            f"**Top requesters:** {top_text}",
            "",
            "**Recent tracks**",
            *(reversed(recent_titles) if recent_titles else ["No tracks played yet."]),
        ]
        view = BaseCardView(timeout=90, author_id=interaction.user.id, guild_id=guild_id, kind="session_recap")
        view.add_item(create_card_container(TextDisplay("\n".join(lines))))
        await reply_card(interaction, view, ephemeral=True)

    def _voice(self, interaction: discord.Interaction) -> tuple[discord.Member, discord.VoiceChannel | discord.StageChannel]:
        member = guild_member(interaction)
        channel = user_voice_channel(interaction)
        if isinstance(channel, discord.StageChannel):
            raise StageUnsupported()
        player = self.bot.registry.get(interaction.guild_id or 0)
        if (
            player
            and player.voice_channel_id != channel.id
            and self.bot.backend.voice_connected(interaction.guild_id or 0, player.voice_channel_id)
        ):
            raise WrongChannel(channel_id=player.voice_channel_id)
        check_bot_channel_permissions(interaction, voice_channel=channel)
        return member, channel

    @app_commands.command(name="playrandom", description="Play a random track from favorites or bot history")
    @app_commands.describe(
        source="Choose favorites or your recent history", continuous="Keep playing random tracks after the queue ends"
    )
    @app_commands.choices(
        source=[
            app_commands.Choice(name="Your recent history", value="history"),
            app_commands.Choice(name="Your favorites", value="favorites"),
        ]
    )
    @app_commands.guild_only()
    async def playrandom(self, interaction: discord.Interaction, source: str = "history", continuous: bool = True) -> None:
        member, channel = self._voice(interaction)
        await safe_defer(interaction)
        tracks = (
            await self.bot.storage.get_favorites(member.id)
            if source == "favorites"
            else await self.bot.storage.get_recent_bot_history_tracks(member.id, 200)
        )
        if not tracks:
            message = (
                "Your favorites are empty. Add tracks with `/favorites add` first."
                if source == "favorites"
                else "There is no recent play history to select from yet."
            )
            await interaction.followup.send(message, ephemeral=True)
            return
        stored = random.choice(tracks)
        avatar = extract_user_avatar_url(member)
        if parse_spotify_url(stored.uri):
            item = QueueItem.from_spotify(stored, member.id, requester_avatar_url=avatar)
        else:
            item = QueueItem(
                None,
                stored.title,
                stored.duration_ms,
                member.id,
                artist=stored.artist,
                uri=stored.uri,
                query=stored.uri or f"ytsearch:{stored.artist} - {stored.title}",
                requester_avatar_url=avatar,
            )
        player = await self.bot.registry.get_or_create(interaction.guild_id or 0, channel.id, interaction.channel_id or 0)
        await player.enqueue([item])
        player.set_random_play(continuous, source=source, user_id=member.id)
        suffix = " Random mode will keep playing." if continuous else " Random mode is off."
        await interaction.followup.send(
            f"Queued random track **{truncate(stored.title)}**.{suffix}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="profile", description="View your music library profile")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        target = member or interaction.user
        await safe_defer(interaction, ephemeral=True)
        favorites = await self.bot.storage.get_favorites(target.id)
        playlists = await self.bot.storage.list_playlists(target.id)
        history = await self.bot.storage.get_user_play_history(target.id, limit=5)
        try:
            avatar = await asyncio.wait_for(target.display_avatar.with_size(128).read(), timeout=1.0)
        except (discord.HTTPException, OSError, asyncio.TimeoutError):
            avatar = None
        png = await asyncio.to_thread(
            render_profile,
            display_name=target.display_name,
            favorite_count=len(favorites),
            playlist_count=len(playlists),
            recent_tracks=[x.title for x in history],
            avatar=avatar,
        )
        await interaction.followup.send(
            file=discord.File(io.BytesIO(png), filename="profile.png"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="stats", description="View your personal listening statistics")
    @app_commands.describe(user="View another user's listening stats")
    async def stats(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        target = user or interaction.user
        if not await safe_defer(interaction, ephemeral=True):
            return
        stats = await self.bot.storage.get_user_listening_stats(target.id)
        try:
            avatar = await asyncio.wait_for(target.display_avatar.with_size(128).read(), timeout=1.0)
        except (discord.HTTPException, OSError, asyncio.TimeoutError):
            avatar = None
        png = await asyncio.to_thread(
            render_stats,
            display_name=target.display_name,
            total_listening_ms=stats["total_listening_ms"],
            total_tracks_played=stats["total_tracks_played"],
            artists=stats["artists"],
            tracks=stats["tracks"],
            avatar=avatar,
        )
        await interaction.followup.send(
            file=discord.File(io.BytesIO(png), filename="stats.png"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    mewsic = app_commands.Group(name="mewsic", description="Import and export Mewsic playlist files")

    @mewsic.command(name="export", description="Export a saved playlist as Mewsic JSON")
    @app_commands.describe(source="Export a saved playlist or the current server queue", playlist="Your playlist name")
    @app_commands.choices(
        source=[
            app_commands.Choice(name="Saved playlist", value="playlist"),
            app_commands.Choice(name="Current queue", value="queue"),
        ]
    )
    async def mewsic_export(self, interaction: discord.Interaction, source: str, playlist: str | None = None) -> None:
        await safe_defer(interaction, ephemeral=True)
        export_name = playlist or "Queue Export"
        if source == "playlist":
            if not playlist:
                await interaction.followup.send("Choose a playlist name to export.", ephemeral=True)
                return
            tracks = await self.bot.storage.get_playlist_tracks(interaction.user.id, playlist)
        else:
            if not interaction.guild_id:
                await interaction.followup.send("Queue export is only available in a server.", ephemeral=True)
                return
            player = self.bot.registry.get(interaction.guild_id)
            if not player:
                tracks = []
            else:
                queue_items = ([player.current] if player.current else []) + list(player.queue)
                tracks = [StoredTrack(uri=i.uri, title=i.title, artist=i.artist, duration_ms=i.duration_ms) for i in queue_items]
                export_name = "Queue Export"
        if not tracks:
            await interaction.followup.send("There are no tracks to export.", ephemeral=True)
            return
        data = _mewsic(tracks[:MAX_IMPORT_TRACKS], export_name)
        filename = re.sub(r"[^A-Za-z0-9_-]", "_", export_name)[:60] or "playlist"
        await interaction.followup.send(
            "Playlist exported.", file=discord.File(io.BytesIO(data), filename=f"{filename}.mewsic.json"), ephemeral=True
        )

    async def _import(self, interaction: discord.Interaction, file: discord.Attachment, play: bool) -> None:
        if file.size > MAX_IMPORT_BYTES:
            await reply(interaction, "Mewsic files must be 5 MB or smaller.", ephemeral=True)
            return
        voice = self._voice(interaction) if play else None
        await safe_defer(interaction, ephemeral=True)
        try:
            name, tracks = _parse_mewsic(await file.read())
            if voice is not None:
                member, channel = voice
                items = [
                    QueueItem(
                        None,
                        t.title,
                        t.duration_ms,
                        member.id,
                        artist=t.artist,
                        uri=t.uri,
                        query=t.uri or f"ytsearch:{t.artist} - {t.title}",
                        requester_avatar_url=extract_user_avatar_url(member),
                    )
                    for t in tracks
                ]
                player = await self.bot.registry.get_or_create(interaction.guild_id or 0, channel.id, interaction.channel_id or 0)
                result = await player.enqueue(items)
                await interaction.followup.send(f"Queued {result.added} tracks from **{name}**.", ephemeral=True)
                return
            created_name = name
            for suffix in range(2, 100):
                try:
                    await self.bot.storage.create_playlist(interaction.user.id, created_name, self.bot.cfg.max_playlists_per_user)
                    break
                except Exception as exc:
                    if "already exists" not in str(exc):
                        raise
                    created_name = f"{name[:70]} ({suffix})"
            else:
                raise ValueError("Could not choose an unused playlist name.")
            added = 0
            track_limit = self.bot.cfg.max_tracks_per_playlist
            import_tracks = tracks[:track_limit] if track_limit > 0 else tracks
            for track in import_tracks:
                await self.bot.storage.add_playlist_track(
                    interaction.user.id, created_name, track, self.bot.cfg.max_tracks_per_playlist
                )
                added += 1
            await interaction.followup.send(f"Imported {added} tracks into **{created_name}**.", ephemeral=True)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(str(exc)[:500], ephemeral=True)
        except Exception as exc:
            log.warning("Mewsic import failed: %s", type(exc).__name__)
            await interaction.followup.send(
                "Import failed. Check that the file is valid and your playlist storage is available.", ephemeral=True
            )

    @mewsic.command(name="import", description="Import a Mewsic JSON file into your playlists")
    async def mewsic_import(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        await self._import(interaction, file, False)

    @mewsic.command(name="play", description="Queue tracks from a Mewsic JSON file")
    @app_commands.guild_only()
    async def mewsic_play(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        await self._import(interaction, file, True)

    @app_commands.command(name="karaoke", description="Show timed lyrics for the current track")
    @app_commands.guild_only()
    async def karaoke(self, interaction: discord.Interaction) -> None:
        member, _ = self._voice(interaction)
        guild_id = interaction.guild_id or 0
        player = self.bot.registry.get(guild_id)
        if player is None:
            await reply(interaction, "Start playback before enabling karaoke.", ephemeral=True)
            return
        require_dj(
            self.bot,
            self.bot.cfg,
            member,
            dj_role_id=getattr(player, "dj_role_id", 0),
            dj_only=getattr(player, "dj_only", False),
        )
        await safe_defer(interaction)
        active = self._karaoke.pop(guild_id, None)
        if active is not None:
            message, task = active
            task.cancel()
            try:
                await message.delete()
            except discord.HTTPException as exc:
                log.debug("guild=%s karaoke message cleanup failed: %s", guild_id, type(exc).__name__)
            await interaction.followup.send("Karaoke mode disabled.", ephemeral=True)
            return
        current = getattr(player, "current", None)
        encoded = getattr(getattr(current, "track", None), "track", None)
        if not encoded:
            await interaction.followup.send("There is no playable track.", ephemeral=True)
            return
        node_manager = getattr(self.bot.lavalink, "node_manager", None)
        node = node_manager.find_ideal_node() if node_manager else None
        if node is None:
            await interaction.followup.send("The audio server is unavailable.", ephemeral=True)
            return
        try:
            timed = await self._fetch_timed_lyrics(node, encoded)
            if not timed:
                await interaction.followup.send("No timed lyrics found for the current track.", ephemeral=True)
                return
            channel = interaction.channel
            if not isinstance(channel, discord.abc.Messageable):
                await interaction.followup.send("Karaoke needs a message channel.", ephemeral=True)
                return
            message = await channel.send(
                f"**Live Karaoke - {truncate(current.title)}**\n{discord.utils.escape_mentions(timed[0][1])}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            task = self.bot.supervisor.spawn(
                self._run_karaoke(guild_id, player, current, message, timed), name=f"karaoke-{guild_id}"
            )
            if task is None:
                try:
                    await message.delete()
                except discord.HTTPException as exc:
                    log.debug("guild=%s karaoke message cleanup failed: %s", guild_id, type(exc).__name__)
                await interaction.followup.send("Karaoke is unavailable right now.", ephemeral=True)
                return
            self._karaoke[guild_id] = (message, task)
            await interaction.followup.send("Live karaoke enabled.", ephemeral=True)
        except Exception as exc:
            log.warning("guild=%s karaoke failed: %s", guild_id, type(exc).__name__)
            await interaction.followup.send("Karaoke could not retrieve timed lyrics.", ephemeral=True)

    @staticmethod
    async def _fetch_timed_lyrics(node: Any, encoded: str) -> list[tuple[int, str]]:
        data = await node.request("GET", "lyrics", params={"track": encoded, "skipTrackSource": "true"})
        raw_lines = data.get("lines", []) if isinstance(data, dict) else []
        timed: list[tuple[int, str]] = []
        for line in raw_lines:
            if not isinstance(line, dict) or not line.get("line"):
                continue
            try:
                timestamp = int(line.get("timestamp", -1))
            except (TypeError, ValueError, OverflowError):
                continue
            if timestamp >= 0:
                timed.append((timestamp, str(line["line"])))
        timed.sort()
        return timed

    async def _run_karaoke(
        self, guild_id: int, player: Any, current: Any, message: discord.Message, lines: list[tuple[int, str]]
    ) -> None:
        index = -1
        shown_item = current
        waiting_shown = False
        try:
            while self.bot.registry.get(guild_id) is player and not player.destroyed:
                current = player.current
                if current is None:
                    if not waiting_shown:
                        await message.edit(content="**Live Karaoke**\nWaiting for the next track...")
                        waiting_shown = True
                    await asyncio.sleep(5)
                    continue
                if current is not shown_item:
                    waiting_shown = False
                    shown_item = current
                    index = -1
                    encoded = getattr(getattr(current, "track", None), "track", None)
                    node_manager = getattr(self.bot.lavalink, "node_manager", None)
                    node = node_manager.find_ideal_node() if node_manager else None
                    lines = await self._fetch_timed_lyrics(node, encoded) if node and encoded else []
                    if not lines:
                        await message.edit(content=f"**Live Karaoke - {truncate(current.title)}**\nNo timed lyrics found.")
                        await asyncio.sleep(5)
                        continue
                position = int(getattr(player.audio, "position", 0) or 0)
                active = max((i for i, (stamp, _) in enumerate(lines) if stamp <= position), default=0)
                if active != index:
                    index = active
                    snippets = lines[max(0, index - 1) : min(len(lines), index + 3)]
                    body = "\n".join(
                        ("**" if i == index else "") + discord.utils.escape_mentions(line) + ("**" if i == index else "")
                        for i, (_, line) in enumerate(snippets, start=max(0, index - 1))
                    )
                    await message.edit(content=f"**Live Karaoke - {truncate(current.title)}**\n{body}")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except discord.HTTPException as exc:
            log.debug("guild=%s karaoke message update failed: %s", guild_id, type(exc).__name__)
        finally:
            active = self._karaoke.get(guild_id)
            if active is not None and active[0] is message:
                self._karaoke.pop(guild_id, None)
            try:
                await message.delete()
            except discord.HTTPException as exc:
                log.debug("guild=%s karaoke message cleanup failed: %s", guild_id, type(exc).__name__)


async def setup(bot: MusicBot) -> None:
    await bot.add_cog(Features(bot))
