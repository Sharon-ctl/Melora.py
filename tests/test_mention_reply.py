from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from config import load_config
from core.alerts import Alerter
from main import MusicBot


class FakeRole:
    def __init__(self, role_id: int, is_managed: bool = True):
        self.id = role_id
        self._is_managed = is_managed

    def is_bot_managed(self) -> bool:
        return self._is_managed


class FakePermissions:
    def __init__(self, view_channel: bool = True, send_messages: bool = True, send_messages_in_threads: bool = True):
        self.view_channel = view_channel
        self.send_messages = send_messages
        self.send_messages_in_threads = send_messages_in_threads


class FakeChannel(discord.TextChannel):
    def __init__(self, perms: FakePermissions):
        self.id = 12345
        self.name = "general"
        self._perms = perms

    def permissions_for(self, member):
        return self._perms


def make_bot():
    cfg = load_config({
        "DISCORD_TOKEN": "A" * 35,
        "OWNER_ID": "123456789",
        "LAVALINK_HOST": "127.0.0.1",
        "LAVALINK_PORT": "2333",
        "LAVALINK_PASSWORD": "secret_password",
    })
    alerter = Alerter(cfg)
    bot = MusicBot(cfg, alerter)
    bot_user = SimpleNamespace(id=999999, bot=True)
    bot._connection.user = bot_user
    bot._bot_user_id = bot_user.id  # Mimic on_ready caching for hot path
    return bot


def make_message(
    bot,
    *,
    author_id: int = 111111,
    author_bot: bool = False,
    content: str = "",
    mentions: list | None = None,
    role_mentions: list | None = None,
    reference: object | None = None,
    channel_perms: FakePermissions | None = None,
    managed_role: FakeRole | None = None,
):
    if managed_role is None:
        managed_role = FakeRole(888888, is_managed=True)
    bot_member = SimpleNamespace(id=999999, roles=[managed_role])
    guild = SimpleNamespace(id=777777, me=bot_member)
    perms = channel_perms or FakePermissions(view_channel=True, send_messages=True)
    channel = FakeChannel(perms)

    resolved_mentions = mentions or []
    resolved_role_mentions = role_mentions or []

    msg = SimpleNamespace(
        id=555555,
        author=SimpleNamespace(id=author_id, bot=author_bot),
        guild=guild,
        channel=channel,
        content=content,
        mentions=resolved_mentions,
        role_mentions=resolved_role_mentions,
        # raw_mentions / raw_role_mentions mirror discord.py's int-id lists
        raw_mentions=[getattr(m, "id", m) for m in resolved_mentions],
        raw_role_mentions=[getattr(r, "id", r) for r in resolved_role_mentions],
        reference=reference,
        reply=AsyncMock(),
    )
    return msg, managed_role


@pytest.mark.anyio
async def test_user_mention_replies():
    bot = make_bot()
    msg, _ = make_message(bot, mentions=[bot.user])
    await bot.on_message(msg)
    assert msg.reply.called
    am = msg.reply.call_args.kwargs.get("allowed_mentions")
    assert am is not None
    assert am.everyone is False and am.users is False and am.roles is False and am.replied_user is False


@pytest.mark.anyio
async def test_role_mention_replies():
    bot = make_bot()
    managed_role = FakeRole(888888, is_managed=True)
    msg, _ = make_message(bot, role_mentions=[managed_role], managed_role=managed_role)
    await bot.on_message(msg)
    assert msg.reply.called


@pytest.mark.anyio
async def test_mention_with_extra_text_replies():
    bot = make_bot()
    msg, _ = make_message(bot, content=f"hey <@{bot.user.id}> play something", mentions=[bot.user])
    await bot.on_message(msg)
    assert msg.reply.called


@pytest.mark.anyio
async def test_no_mention_ignored():
    bot = make_bot()
    other_user = SimpleNamespace(id=222222, bot=False)
    msg, _ = make_message(bot, content="just chatting", mentions=[other_user])
    await bot.on_message(msg)
    assert not msg.reply.called


@pytest.mark.anyio
async def test_bot_author_ignored():
    bot = make_bot()
    msg, _ = make_message(bot, author_bot=True, mentions=[bot.user])
    await bot.on_message(msg)
    assert not msg.reply.called


@pytest.mark.anyio
async def test_no_permission_ignored():
    bot = make_bot()
    perms = FakePermissions(view_channel=True, send_messages=False)
    msg, _ = make_message(bot, mentions=[bot.user], channel_perms=perms)
    await bot.on_message(msg)
    assert not msg.reply.called


@pytest.mark.anyio
async def test_cooldown_enforced():
    bot = make_bot()
    msg1, _ = make_message(bot, author_id=333333, mentions=[bot.user])
    msg2, _ = make_message(bot, author_id=333333, mentions=[bot.user])
    await bot.on_message(msg1)
    assert msg1.reply.called

    await bot.on_message(msg2)
    assert not msg2.reply.called


@pytest.mark.anyio
async def test_reply_ping_without_explicit_mention_ignored():
    bot = make_bot()
    msg, _ = make_message(
        bot,
        content="thanks for the info",
        mentions=[bot.user],
        reference=SimpleNamespace(message_id=444444),
    )
    await bot.on_message(msg)
    assert not msg.reply.called


@pytest.mark.anyio
async def test_reply_ping_with_explicit_mention_replies():
    bot = make_bot()
    msg, _ = make_message(
        bot,
        content=f"<@{bot.user.id}> thanks for the info",
        mentions=[bot.user],
        reference=SimpleNamespace(message_id=444444),
    )
    await bot.on_message(msg)
    assert msg.reply.called
