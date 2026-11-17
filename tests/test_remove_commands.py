import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from core.queue import QueueItem, TrackQueue
from tests.fakes import make_config, make_track
from utils import messages


def _item(user: int, title: str) -> QueueItem:
    return QueueItem(track=make_track(1), title=title, duration_ms=1000, requester_id=user)


def test_removed_mine_message_grammar():
    assert messages.removed_mine(1) == "Removed **1 track** you added from the queue"
    assert messages.removed_mine(3) == "Removed **3 tracks** you added from the queue"
    assert "nothing queued" in messages.removed_mine(0)


def test_removed_user_message_escapes_name_and_handles_zero():
    assert messages.removed_user(2, "Sam") == "Removed **2 tracks** added by **Sam**"
    assert messages.removed_user(1, "Sam") == "Removed **1 track** added by **Sam**"
    assert "has nothing queued" in messages.removed_user(0, "Sam")
    assert "\\*" in messages.removed_user(1, "*bold*")


def _make_inter(user_id: int = 111) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    inter.guild_id = 999
    inter.extras = {}
    inter.user = MagicMock(spec=discord.Member)
    inter.user.id = user_id
    return inter


def _make_cog():
    from cogs.music import Music
    from core.alerts import Alerter
    from main import MusicBot

    cfg = make_config()
    return Music(MusicBot(cfg, Alerter(cfg)))


def test_remove_mine_command_only_removes_callers_tracks():
    async def scenario():
        cog = _make_cog()
        queue = TrackQueue(max_size=10, max_per_user=10)
        for user, title in [(111, "A"), (222, "B"), (111, "C")]:
            queue.add(_item(user, title))
        player = MagicMock()
        player.remove_by_user = AsyncMock(side_effect=lambda uid: queue.remove_by_user(uid))
        with (
            patch.object(cog, "_control_gate", return_value=player) as gate,
            patch("cogs.music.reply", new_callable=AsyncMock) as mock_reply,
        ):
            await cog.remove_mine.callback(cog, _make_inter(111))
        gate.assert_called_once()
        assert gate.call_args.kwargs.get("dj", False) is False
        assert [i.title for i in queue] == ["B"]
        mock_reply.assert_awaited_once()
        assert mock_reply.call_args.args[1] == messages.removed_mine(2)

    asyncio.run(scenario())


def test_remove_user_command_is_dj_gated_and_targets_member():
    async def scenario():
        cog = _make_cog()
        queue = TrackQueue(max_size=10, max_per_user=10)
        for user, title in [(111, "A"), (222, "B"), (222, "C")]:
            queue.add(_item(user, title))
        player = MagicMock()
        player.remove_by_user = AsyncMock(side_effect=lambda uid: queue.remove_by_user(uid))
        target = MagicMock(spec=discord.Member)
        target.id = 222
        target.display_name = "Sam"
        with (
            patch.object(cog, "_control_gate", return_value=player) as gate,
            patch("cogs.music.reply", new_callable=AsyncMock) as mock_reply,
        ):
            await cog.remove_user.callback(cog, _make_inter(111), target)
        assert gate.call_args.kwargs.get("dj") is True
        assert [i.title for i in queue] == ["A"]
        assert mock_reply.call_args.args[1] == messages.removed_user(2, "Sam")

    asyncio.run(scenario())
