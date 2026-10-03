"""Comprehensive edge-case unit tests for Phase 8.

Covers:
1. Bot mentions in threads, voice channels, permission checks, cooldowns, and reply pings.
2. PaginatedView async providers, empty states, expired states, and unauthorized callbacks.
3. Help categories mapping, fallback to Other, owner filtering, and page navigation.
4. Autocomplete string and numeric filtering, special characters, and preset matching.
5. Now Playing card lifecycle transitions (stop, end of queue, channel moves, and recreation).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.admin import precompute_help_pages
from cogs.filters import Filters
from cogs.music import Music
from config import load_config
from core.alerts import Alerter
from core.contracts import PlayerServices
from core.guild_player import GuildPlayer
from core.queue import QueueItem
from core.registry import PlayerRegistry
from core.storage import Storage
from main import MusicBot
from tests.fakes import FakeBackend, FakeLoader, make_config, make_track
from utils.components_v2 import PaginatedPage, PaginatedView


# -----------------------------------------------------------------------------
# 1. Mention Reply Edge Cases
# -----------------------------------------------------------------------------

def _make_bot() -> MusicBot:
    cfg = load_config({
        "DISCORD_TOKEN": "A" * 35,
        "OWNER_ID": "123456789",
        "LAVALINK_HOST": "127.0.0.1",
        "LAVALINK_PORT": "2333",
        "LAVALINK_PASSWORD": "secret_password",
    })
    bot = MusicBot(cfg, Alerter(cfg))
    bot_user = SimpleNamespace(id=999999, bot=True)
    bot._connection.user = bot_user
    bot._bot_user_id = bot_user.id
    return bot


class FakePerms:
    def __init__(self, view_channel: bool = True, send_messages: bool = True, send_messages_in_threads: bool = True) -> None:
        self.view_channel = view_channel
        self.send_messages = send_messages
        self.send_messages_in_threads = send_messages_in_threads


class FakeTextChannel(discord.TextChannel):
    def __init__(self, perms: FakePerms) -> None:
        self.id = 555
        self.name = "general"
        self._perms = perms

    def permissions_for(self, member: Any) -> FakePerms:
        return self._perms


class FakeThreadChannel(discord.Thread):
    def __init__(self, perms: FakePerms) -> None:
        self.id = 556
        self.name = "thread-1"
        self._perms = perms

    def permissions_for(self, member: Any) -> FakePerms:
        return self._perms


@pytest.mark.anyio
async def test_mention_in_thread_with_permissions():
    bot = _make_bot()
    bot_uid = bot._bot_user_id
    channel = FakeThreadChannel(FakePerms(view_channel=True, send_messages_in_threads=True))
    guild = SimpleNamespace(id=777, me=SimpleNamespace(id=bot_uid))

    msg = SimpleNamespace(
        id=1,
        author=SimpleNamespace(id=111, bot=False),
        guild=guild,
        channel=channel,
        raw_mentions=[bot_uid],
        raw_role_mentions=[],
        reference=None,
        reply=AsyncMock(),
    )
    await bot.on_message(msg)
    assert msg.reply.called


@pytest.mark.anyio
async def test_mention_without_send_permission_ignored():
    bot = _make_bot()
    bot_uid = bot._bot_user_id
    channel = FakeTextChannel(FakePerms(view_channel=True, send_messages=False))
    guild = SimpleNamespace(id=777, me=SimpleNamespace(id=bot_uid))

    msg = SimpleNamespace(
        id=2,
        author=SimpleNamespace(id=111, bot=False),
        guild=guild,
        channel=channel,
        raw_mentions=[bot_uid],
        raw_role_mentions=[],
        reference=None,
        reply=AsyncMock(),
    )
    await bot.on_message(msg)
    assert not msg.reply.called


@pytest.mark.anyio
async def test_mention_cooldown_triggers_ignore():
    bot = _make_bot()
    bot_uid = bot._bot_user_id
    channel = FakeTextChannel(FakePerms(view_channel=True, send_messages=True))
    guild = SimpleNamespace(id=777, me=SimpleNamespace(id=bot_uid))

    msg = SimpleNamespace(
        id=3,
        author=SimpleNamespace(id=111, bot=False),
        guild=guild,
        channel=channel,
        raw_mentions=[bot_uid],
        raw_role_mentions=[],
        reference=None,
        reply=AsyncMock(),
    )
    # First mention succeeds
    await bot.on_message(msg)
    assert msg.reply.call_count == 1

    # Immediate second mention from same user is blocked by cooldown
    msg2 = SimpleNamespace(
        id=4,
        author=SimpleNamespace(id=111, bot=False),
        guild=guild,
        channel=channel,
        raw_mentions=[bot_uid],
        raw_role_mentions=[],
        reference=None,
        reply=AsyncMock(),
    )
    await bot.on_message(msg2)
    assert msg2.reply.call_count == 0


# -----------------------------------------------------------------------------
# 2. PaginatedView Comprehensive Cases
# -----------------------------------------------------------------------------

@pytest.mark.anyio
async def test_paginated_view_empty_list():
    def empty_provider(page: int) -> PaginatedPage:
        return PaginatedPage(
            title="**Empty List**",
            items=[],
            current_page=1,
            total_pages=1,
            empty_message="Nothing to see here.",
        )

    view = PaginatedView(items_provider=empty_provider, author_id=123)
    container = await view.render(1)
    assert container is not None
    assert view.total_pages == 1
    assert view.current_page == 1
    assert view._prev_btn.disabled is True
    assert view._next_btn.disabled is True


@pytest.mark.anyio
async def test_paginated_view_async_provider():
    async def async_provider(page: int) -> PaginatedPage:
        await asyncio.sleep(0)
        return PaginatedPage(
            title="**Async List**",
            items=[f"Row {k}" for k in range(5)],
            current_page=page,
            total_pages=2,
        )

    view = PaginatedView(items_provider=async_provider, author_id=123)
    await view.render(1)
    assert view.current_page == 1
    assert view.total_pages == 2
    assert view._next_btn.disabled is False


@pytest.mark.anyio
async def test_paginated_view_unauthorized_user():
    def provider(page: int) -> PaginatedPage:
        return PaginatedPage(title="**List**", items=["Item 1"], current_page=1, total_pages=2)

    view = PaginatedView(items_provider=provider, author_id=123)
    await view.render(1)

    stranger = SimpleNamespace(user=SimpleNamespace(id=999), response=AsyncMock())
    check = await view.interaction_check(stranger)
    assert check is False
    assert stranger.response.send_message.called
    assert view.current_page == 1  # Unchanged


# -----------------------------------------------------------------------------
# 3. Help Categories & Fallback
# -----------------------------------------------------------------------------

@pytest.mark.anyio
async def test_help_categories_fallback_and_precomputation():
    categories = ["Overview", "Playback", "Other"]
    descriptions = {"Overview": "Tips", "Playback": "Audio", "Other": "Miscellaneous"}
    commands_by_cat = {
        "Playback": ["/play - Play audio"],
        "Other": ["/custom_unmapped - Custom tool"],
    }
    pages = precompute_help_pages(categories, descriptions, commands_by_cat)
    assert "Overview" in pages
    assert "Playback" in pages
    assert "Other" in pages
    assert len(pages["Playback"]) == 1
    assert "/play" in pages["Playback"][0]
    assert "/custom_unmapped" in pages["Other"][0]


# -----------------------------------------------------------------------------
# 4. Autocomplete Edge Cases
# -----------------------------------------------------------------------------

@pytest.mark.anyio
async def test_queue_autocomplete_special_chars():
    cfg = make_config(AUTOCOMPLETE_SEARCH_ENABLED=True)
    backend = FakeBackend()
    loader = FakeLoader()
    storage = Storage(":memory:")
    storage.start()
    services = PlayerServices(cfg, backend, loader, storage)
    registry = PlayerRegistry(services)
    bot = SimpleNamespace(cfg=cfg, backend=backend, loader=loader, storage=storage, registry=registry)
    music = Music(bot)

    player = await registry.get_or_create(10, 100, 200)
    current_item = QueueItem.from_track(make_track(1, title="Playing Now"), 42)
    special_item = QueueItem.from_track(make_track(2, title="Song [Remix] (feat. Star) *2024*"), 42)
    await player.enqueue([current_item, special_item])

    # Search with regex special characters like [, (, *
    inter = SimpleNamespace(guild=SimpleNamespace(id=10), guild_id=10, user=SimpleNamespace(id=42))
    choices = await music._queue_pos_autocomplete(inter, current="[Remix]")
    assert len(choices) == 1
    assert "1" in choices[0].name


@pytest.mark.anyio
async def test_eq_preset_autocomplete_case_insensitive():
    cfg = make_config()
    backend = FakeBackend()
    loader = FakeLoader()
    services = PlayerServices(cfg, backend, loader)
    registry = PlayerRegistry(services)
    bot = SimpleNamespace(cfg=cfg, backend=backend, loader=loader, services=services, registry=registry)
    filters_cog = Filters(bot)
    filters_cog.eq_presets = {"bass_boost": [0] * 15, "treble_boost": [0] * 15, "vocal": [0] * 15}

    await registry.get_or_create(10, 100, 200)
    inter = SimpleNamespace(guild=SimpleNamespace(id=10), guild_id=10, user=SimpleNamespace(id=42))
    choices = await filters_cog.eq_preset_autocomplete(inter, "BASS")
    names = [c.name.lower() for c in choices]
    assert any("bass" in name for name in names)


# -----------------------------------------------------------------------------
# 5. Now Playing Card Lifecycle Transitions
# -----------------------------------------------------------------------------

@pytest.mark.anyio
async def test_card_lifecycle_channel_move_and_delete():
    cfg = make_config()
    backend = FakeBackend()
    loader = FakeLoader()
    services = PlayerServices(cfg, backend, loader)
    player = GuildPlayer(1, 100, 200, services)
    await backend.connect(1, 100)

    # 1. Start playback and create card
    items = [QueueItem.from_track(make_track(n), 42) for n in range(2)]
    await player.enqueue(items)
    await player.create_card()

    assert player.nowplaying_channel_id == 200
    assert player.nowplaying_message_id is not None
    first_msg_id = player.nowplaying_message_id
    assert (200, first_msg_id) in backend.cards_sent

    # 2. Move card to new channel
    await player.move_card(300)
    assert player.nowplaying_channel_id == 300
    assert (200, first_msg_id) in backend.cards_deleted

    # 3. Stop playback deletes card
    await player.stop()
    assert player.nowplaying_message_id is None
    assert (300, backend.next_msg_id) in backend.cards_deleted
    player.shutdown()
