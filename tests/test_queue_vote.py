import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from core.queue import QueueItem, TrackQueue
from tests.fakes import make_config, make_track

VOICE_CHANNEL_ID = 500


def _make_cog(humans: int):
    from cogs.queue_cmds import QueueCommands
    from core.alerts import Alerter
    from main import MusicBot

    cfg = make_config()
    bot = MusicBot(cfg, Alerter(cfg))
    bot.backend = MagicMock()
    bot.backend.humans_in_channel.return_value = humans
    bot.registry = MagicMock()
    return QueueCommands(bot), bot


def _make_player(titles):
    queue = TrackQueue(max_size=20, max_per_user=20)
    for title in titles:
        queue.add(QueueItem(track=make_track(1), title=title, duration_ms=1000, requester_id=1))
    player = MagicMock()
    player.queue = queue
    player.voice_channel_id = VOICE_CHANNEL_ID
    player.move_item_to_front = AsyncMock(return_value=True)
    return player


def _make_inter(listeners: set[int]):
    inter = MagicMock(spec=discord.Interaction)
    inter.guild_id = 999
    voice_channel = MagicMock()
    voice_channel.voice_states = dict.fromkeys(listeners, MagicMock())
    inter.guild = MagicMock()
    inter.guild.get_channel.return_value = voice_channel
    return inter


def _make_member(user_id: int):
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.voice = MagicMock()
    member.voice.channel = MagicMock()
    member.voice.channel.id = VOICE_CHANNEL_ID
    return member


def test_queue_vote_works_without_guild_voice_states_and_passes_on_majority():
    """discord.Guild has no ``voice_states``; the vote must read the voice channel instead."""

    async def scenario():
        cog, bot = _make_cog(humans=3)
        player = _make_player(["now", "second", "third"])
        bot.registry.get = MagicMock(return_value=player)
        inter = _make_inter({10, 11, 12})

        with patch("cogs.queue_cmds.reply", new_callable=AsyncMock) as mock_reply:
            with patch("cogs.queue_cmds.guild_member", return_value=_make_member(10)):
                await cog.queue_vote.callback(cog, inter, 3)
            assert "1/2" in mock_reply.call_args.args[1]
            player.move_item_to_front.assert_not_awaited()

            with patch("cogs.queue_cmds.guild_member", return_value=_make_member(11)):
                await cog.queue_vote.callback(cog, inter, 3)
            assert "Vote passed" in mock_reply.call_args.args[1]
            player.move_item_to_front.assert_awaited_once()

    asyncio.run(scenario())


def test_queue_vote_ignores_votes_from_members_who_left_the_channel():
    async def scenario():
        cog, bot = _make_cog(humans=3)
        player = _make_player(["now", "second", "third"])
        bot.registry.get = MagicMock(return_value=player)

        with patch("cogs.queue_cmds.reply", new_callable=AsyncMock) as mock_reply:
            with patch("cogs.queue_cmds.guild_member", return_value=_make_member(10)):
                await cog.queue_vote.callback(cog, _make_inter({10, 11, 12}), 3)
            # Voter 10 leaves before voter 11 votes, so only one valid vote remains.
            with patch("cogs.queue_cmds.guild_member", return_value=_make_member(11)):
                await cog.queue_vote.callback(cog, _make_inter({11, 12, 13}), 3)
            assert "1/2" in mock_reply.call_args.args[1]
            player.move_item_to_front.assert_not_awaited()

    asyncio.run(scenario())


def test_queue_vote_rejects_duplicate_vote_and_next_track():
    async def scenario():
        cog, bot = _make_cog(humans=5)
        player = _make_player(["now", "second", "third"])
        bot.registry.get = MagicMock(return_value=player)
        inter = _make_inter({10, 11, 12, 13, 14})

        with (
            patch("cogs.queue_cmds.reply", new_callable=AsyncMock) as mock_reply,
            patch("cogs.queue_cmds.guild_member", return_value=_make_member(10)),
        ):
            await cog.queue_vote.callback(cog, inter, 3)
            await cog.queue_vote.callback(cog, inter, 3)
            assert "already voted" in mock_reply.call_args.args[1]
            await cog.queue_vote.callback(cog, inter, 1)
            assert "already next" in mock_reply.call_args.args[1]

    asyncio.run(scenario())
