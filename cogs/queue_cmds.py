"""Queue management slash commands. All commands are guild-only."""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from cogs.base import PlayerCog
from core.queue import LoopMode, QueueItem
from core.storage import StorageError, StoredTrack
from utils import messages
from utils.checks import guild_member
from utils.components_v2 import (
    PaginatedPage,
    PaginatedView,
    reply_card,
)
from utils.errors import (
    BotUserError,
    NothingPlaying,
    WrongChannel,
)
from utils.interaction import reply
from utils.text import clean, format_duration, truncate

if TYPE_CHECKING:
    from main import MusicBot

log = logging.getLogger(__name__)

QUEUE_PAGE_SIZE = 10


def estimate_queue_wait_ms(
    current_duration_ms: int, current_position_ms: int, current_is_stream: bool, queued: list[QueueItem], position: int
) -> tuple[int, bool]:
    """Return estimated start delay and whether a live stream was omitted."""
    preceding = queued[: position - 1]
    omitted_stream = current_is_stream or any(item.is_stream for item in preceding)
    remaining = 0 if current_is_stream else max(0, current_duration_ms - max(0, current_position_ms))
    remaining += sum(max(0, item.duration_ms) for item in preceding if not item.is_stream)
    return remaining, omitted_stream


class QueueCommands(PlayerCog):
    def __init__(self, bot: MusicBot) -> None:
        super().__init__(bot)
        self._queue_vote_state: dict[int, tuple[QueueItem, set[int], float]] = {}

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

    @app_commands.command(name="queue-forecast", description="Estimate when a queued track will start")
    @app_commands.describe(position="Position shown by /queue")
    @app_commands.guild_only()
    async def queue_forecast(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 1000]) -> None:
        player = self.bot.registry.get(interaction.guild_id or 0)
        if player is None:
            raise NothingPlaying()
        queued = list(player.queue)
        if position > len(queued):
            raise BotUserError("That position is not in the current queue.")
        audio = player.audio
        played_ms = max(0, int(getattr(audio, "position", 0) or 0)) if audio is not None else 0
        estimate, omitted_stream = estimate_queue_wait_ms(
            player.current.duration_ms if player.current else 0,
            played_ms,
            bool(player.current and player.current.is_stream),
            queued,
            position,
        )
        suffix = " (excluding live streams)" if omitted_stream else ""
        await reply(
            interaction,
            f"Track **#{position}** is estimated to start in about `{format_duration(estimate)}`{suffix}.",
            ephemeral=True,
        )

    @app_commands.command(name="queue-balance", description="Show how upcoming tracks are distributed by requester")
    @app_commands.guild_only()
    async def queue_balance(self, interaction: discord.Interaction) -> None:
        player = self.bot.registry.get(interaction.guild_id or 0)
        if player is None or not player.queue:
            await reply(interaction, "There are no upcoming tracks to balance.", ephemeral=True)
            return
        counts: dict[int, int] = {}
        for item in player.queue:
            counts[item.requester_id] = counts.get(item.requester_id, 0) + 1
        ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        page_size = 10
        page_count = max(1, (len(ordered) + page_size - 1) // page_size)

        def balance_page(page_number: int) -> PaginatedPage:
            page_number = min(max(1, page_number), page_count)
            start = (page_number - 1) * page_size
            entries = ordered[start : start + page_size]
            items = [
                f"{'<@' + str(user_id) + '>' if user_id else 'Unknown requester'} - "
                f"**{count}** track{'s' if count != 1 else ''}"
                for user_id, count in entries
            ]
            return PaginatedPage(
                title="**Upcoming Queue Balance**",
                items=items,
                current_page=page_number,
                total_pages=page_count,
                extra_header=f"Total: {len(player.queue)} upcoming tracks",
            )

        view = PaginatedView(
            items_provider=balance_page,
            author_id=interaction.user.id,
            guild_id=interaction.guild_id or 0,
            kind="queue_balance",
        )
        await view.render(1)
        await reply_card(interaction, view, ephemeral=True)

    @app_commands.command(name="queue-vote", description="Vote to move an upcoming track to the front")
    @app_commands.describe(position="Track position shown by /queue")
    @app_commands.guild_only()
    async def queue_vote(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 1000]) -> None:
        member = guild_member(interaction)
        player = self.bot.registry.get(interaction.guild_id or 0)
        if player is None:
            raise NothingPlaying()
        if member.voice is None or member.voice.channel is None or member.voice.channel.id != player.voice_channel_id:
            raise WrongChannel(channel_id=player.voice_channel_id)
        if position > len(player.queue):
            raise BotUserError("That position is not in the current queue.")
        if position == 1:
            await reply(interaction, "That track is already next.", ephemeral=True)
            return
        guild_id = interaction.guild_id or 0
        now = time.monotonic()
        for stale_guild, stale_state in list(self._queue_vote_state.items()):
            if now - stale_state[2] > 60:
                self._queue_vote_state.pop(stale_guild, None)
        target = player.queue[position - 1]
        state = self._queue_vote_state.get(guild_id)
        if state is None or state[0] is not target or now - state[2] > 60:
            votes: set[int] = set()
        else:
            votes = state[1]
        guild = interaction.guild
        voice_channel = guild.get_channel(player.voice_channel_id) if guild is not None else None
        channel_voice_states = getattr(voice_channel, "voice_states", None)
        if channel_voice_states is not None:
            # Drop votes from members who have left the bot's voice channel.
            votes.intersection_update(set(channel_voice_states))
        if member.id in votes:
            await reply(interaction, "You already voted for that track.", ephemeral=True)
            return
        votes.add(member.id)
        humans = self.bot.backend.humans_in_channel(guild_id, player.voice_channel_id) or 1
        needed = max(1, humans // 2 + 1)
        if len(votes) >= needed:
            moved = await player.move_item_to_front(target)
            self._queue_vote_state.pop(guild_id, None)
            if moved:
                await reply(interaction, f"Vote passed. **{truncate(target.title)}** is now next.")
            else:
                await reply(interaction, "That track left the queue before the vote passed.", ephemeral=True)
        else:
            self._queue_vote_state[guild_id] = (target, votes, now)
            await reply(interaction, f"Vote recorded: **{len(votes)}/{needed}** needed to move it next.", ephemeral=True)

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

    @app_commands.command(name="remove-mine", description="Remove all upcoming tracks you added to the queue")
    @app_commands.guild_only()
    async def remove_mine(self, interaction: discord.Interaction) -> None:
        player = self._control_gate(interaction)
        removed = await player.remove_by_user(interaction.user.id)
        await reply(interaction, messages.removed_mine(len(removed)))

    @app_commands.command(name="remove-user", description="DJ: remove all upcoming tracks added by a member")
    @app_commands.describe(user="Member whose queued tracks should be removed")
    @app_commands.guild_only()
    async def remove_user(self, interaction: discord.Interaction, user: discord.Member) -> None:
        player = self._control_gate(interaction, dj=True)
        removed = await player.remove_by_user(user.id)
        await reply(interaction, messages.removed_user(len(removed), user.display_name))

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
    await bot.add_cog(QueueCommands(bot))
