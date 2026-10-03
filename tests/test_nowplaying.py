"""Tests for Phase 5: Now Playing Card lifecycle."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.contracts import PlayerServices
from core.guild_player import GuildPlayer
from core.queue import LoopMode, QueueItem
from tests.fakes import FakeBackend, FakeLoader, make_config, make_track


GUILD = 1
VOICE = 100
TEXT = 200


def _services(backend: FakeBackend | None = None) -> PlayerServices:
    return PlayerServices(
        cfg=make_config(),
        backend=backend or FakeBackend(),
        loader=FakeLoader(),
    )


def _items(count: int, user: int = 42) -> list[QueueItem]:
    return [QueueItem.from_track(make_track(n), user) for n in range(count)]


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


async def _create_playing(b: FakeBackend | None = None) -> tuple[GuildPlayer, FakeBackend]:
    """Create player, enqueue, fire track start, let card task run."""
    if b is None:
        b = FakeBackend()
    svc = _services(b)
    player = GuildPlayer(GUILD, VOICE, TEXT, svc)
    await b.connect(GUILD, VOICE)
    await player.enqueue(_items(3))
    # Simulate what Lavalink does: fire track start
    await player.on_track_start()
    # Let the spawned card-create task actually run
    await asyncio.sleep(0.05)
    return player, b


# ---------------------------------------------------------------- card lifecycle


def test_card_created_on_first_track():
    async def scenario():
        player, b = await _create_playing()
        assert len(b.cards_sent) >= 1
        assert player.nowplaying_channel_id == TEXT
        assert player.nowplaying_message_id is not None
        player.shutdown()

    run(scenario())


def test_card_updated_on_pause_resume():
    async def scenario():
        player, b = await _create_playing()
        initial_edits = len(b.cards_edited)
        await player.pause()
        # Let the coalesced update run (1s debounce)
        await asyncio.sleep(1.2)
        assert len(b.cards_edited) > initial_edits
        edits_after_pause = len(b.cards_edited)
        await player.resume()
        await asyncio.sleep(1.2)
        assert len(b.cards_edited) > edits_after_pause
        player.shutdown()

    run(scenario())


def test_card_updated_on_loop_change():
    async def scenario():
        player, b = await _create_playing()
        initial_edits = len(b.cards_edited)
        player.set_loop(LoopMode.TRACK)
        await asyncio.sleep(1.2)
        assert len(b.cards_edited) > initial_edits
        player.shutdown()

    run(scenario())


def test_card_deleted_on_stop():
    async def scenario():
        player, b = await _create_playing()
        assert player.nowplaying_message_id is not None
        await player.stop()
        await asyncio.sleep(0.05)
        assert player.nowplaying_message_id is None
        assert player.nowplaying_channel_id is None
        assert len(b.cards_deleted) >= 1
        player.shutdown()

    run(scenario())


def test_card_deleted_on_queue_end():
    async def scenario():
        b = FakeBackend()
        svc = _services(b)
        player = GuildPlayer(GUILD, VOICE, TEXT, svc)
        await b.connect(GUILD, VOICE)
        await player.enqueue(_items(1))
        await player.on_track_start()
        await asyncio.sleep(0.05)
        assert player.nowplaying_message_id is not None
        # Simulate track end -> advance finds nothing
        await player.on_track_end(None, "finished")
        await asyncio.sleep(0.1)
        assert player.current is None
        # Card delete is spawned as a task
        await asyncio.sleep(0.05)
        assert len(b.cards_deleted) >= 1
        player.shutdown()

    run(scenario())


def test_card_moved_to_new_channel():
    async def scenario():
        player, b = await _create_playing()
        old_msg_id = player.nowplaying_message_id
        new_channel = 999
        await player.move_card(new_channel)
        await asyncio.sleep(0.05)
        assert len(b.cards_deleted) >= 1
        assert player.nowplaying_channel_id == new_channel
        assert player.nowplaying_message_id is not None
        assert player.nowplaying_message_id != old_msg_id
        player.shutdown()

    run(scenario())


def test_shutdown_stops_view_and_clears_state():
    async def scenario():
        player, b = await _create_playing()
        assert player.nowplaying_view is not None
        player.shutdown()
        assert player.nowplaying_view is None
        assert player.nowplaying_channel_id is None
        assert player.nowplaying_message_id is None

    run(scenario())


def test_card_not_created_when_send_fails():
    async def scenario():
        b = FakeBackend()
        b.fail_send = True
        svc = _services(b)
        player = GuildPlayer(GUILD, VOICE, TEXT, svc)
        await b.connect(GUILD, VOICE)
        await player.enqueue(_items(1))
        await player.on_track_start()
        await asyncio.sleep(0.05)
        assert player.nowplaying_message_id is None
        assert player.nowplaying_view is None
        player.shutdown()

    run(scenario())


def test_card_recreated_on_edit_not_found():
    async def scenario():
        player, b = await _create_playing()
        old_msg_id = player.nowplaying_message_id
        assert old_msg_id is not None
        sent_before = len(b.cards_sent)
        # Simulate that the next edit will raise NotFound
        b.raise_not_found_on_edit = True
        await player.pause()
        await asyncio.sleep(1.5)
        # The card should have been recreated (new send)
        assert len(b.cards_sent) > sent_before
        player.shutdown()

    run(scenario())


def test_coalesce_avoids_rapid_edits():
    """Multiple state changes within 1s should produce at most one edit."""
    async def scenario():
        player, b = await _create_playing()
        initial_edits = len(b.cards_edited)
        player.set_loop(LoopMode.TRACK)
        player.set_loop(LoopMode.QUEUE)
        player.set_loop(LoopMode.OFF)
        # All three fired within the same tick; coalescing should batch them
        await asyncio.sleep(1.5)
        new_edits = len(b.cards_edited) - initial_edits
        # Should be exactly 1 (coalesced) rather than 3
        assert new_edits == 1
        player.shutdown()

    run(scenario())


# ---------------------------------------------------------------- container builder


def test_build_container_no_artwork():
    from utils.components_v2 import build_nowplaying_container

    container = build_nowplaying_container(
        title="Test Song",
        artist="Test Artist",
        duration_str="3:30",
        requester_name="Alice",
        artwork_url=None,
        is_paused=False,
        is_stream=False,
        loop_mode="off",
        queue_size=5,
    )
    assert container is not None


def test_build_container_with_artwork():
    from utils.components_v2 import build_nowplaying_container

    container = build_nowplaying_container(
        title="Test Song",
        artist="Test Artist",
        duration_str="3:30",
        requester_name="Bob",
        artwork_url="https://example.com/art.jpg",
        is_paused=True,
        is_stream=False,
        loop_mode="track",
        queue_size=0,
    )
    assert container is not None


def test_build_container_live_stream():
    from utils.components_v2 import build_nowplaying_container

    container = build_nowplaying_container(
        title="Live Stream",
        artist="",
        duration_str="0:00",
        requester_name="",
        artwork_url=None,
        is_paused=False,
        is_stream=True,
        loop_mode="queue",
        queue_size=10,
    )
    assert container is not None


# ---------------------------------------------------------------- QueueItem artwork


def test_from_track_picks_artwork_url():
    track = SimpleNamespace(
        title="Song", author="Artist", duration=180000,
        is_stream=False, track="enc1", uri="http://x",
        artwork_url="https://example.com/pic.jpg",
    )
    item = QueueItem.from_track(track, 42, requester_name="Alice")
    assert item.artwork_url == "https://example.com/pic.jpg"
    assert item.requester_name == "Alice"


def test_from_track_no_artwork():
    item = QueueItem.from_track(make_track(1), 42)
    assert item.artwork_url is None
    assert item.requester_name == ""


def test_replace_track_updates_artwork():
    item = QueueItem.from_track(make_track(1), 42)
    assert item.artwork_url is None
    new_track = SimpleNamespace(
        title="New Song", author="Artist", duration=200000,
        is_stream=False, track="enc2", uri="http://y",
        artwork_url="https://example.com/new.jpg",
    )
    item.replace_track(new_track)
    assert item.artwork_url == "https://example.com/new.jpg"


# ---------------------------------------------------------------- Phase 5 card builder tests


def test_card_builder_header_uses_live_bot_name():
    from utils.components_v2 import build_nowplaying_container, Section, TextDisplay

    container = build_nowplaying_container(
        bot_name="LiveBotName",
        title="Sample Song",
        uri="https://example.com/song",
        requester_id=123,
        duration_str="4:15",
        requester_avatar_url="https://example.com/avatar.png",
    )
    section = [c for c in container.children if isinstance(c, Section)][0]
    texts = [c for c in section.children if isinstance(c, TextDisplay)]
    assert texts[0].content == "**LiveBotName**"


def test_card_builder_thumbnail_is_artwork():
    from utils.components_v2 import build_nowplaying_container, Section, Thumbnail

    art = "https://example.com/artwork.jpg"
    container = build_nowplaying_container(
        bot_name="Melora",
        title="Song with Artwork",
        uri="https://example.com",
        requester_id=42,
        duration_str="2:50",
        artwork_url=art,
    )
    section = [c for c in container.children if isinstance(c, Section)][0]
    assert isinstance(section.accessory, Thumbnail)
    assert section.accessory.media.url == art


def test_card_builder_no_user_avatar():
    from utils.components_v2 import build_nowplaying_container, Section

    avatar = "https://cdn.discordapp.com/avatars/42/requester.png"
    art = "https://example.com/artwork.jpg"
    container = build_nowplaying_container(
        bot_name="Melora",
        title="Song",
        uri="https://example.com",
        requester_id=42,
        duration_str="2:50",
        requester_avatar_url=avatar,
        artwork_url=art,
    )
    comp_dict = container.to_component_dict()
    assert avatar not in str(comp_dict)
    section = [c for c in container.children if isinstance(c, Section)][0]
    assert section.accessory.media.url == art


def test_card_builder_link_escaped_and_clickable():
    from utils.components_v2 import build_nowplaying_container, Section, TextDisplay

    title = "Song [Remix] *cool* & _great_ (Official Video)"
    uri = "https://youtube.com/watch?v=abc123xyz"
    container = build_nowplaying_container(
        bot_name="Melora",
        title=title,
        uri=uri,
        requester_id=42,
        duration_str="3:42",
    )
    section = [c for c in container.children if isinstance(c, Section)][0]
    texts = [c for c in section.children if isinstance(c, TextDisplay)]
    body_text = texts[1].content
    lines = body_text.splitlines()

    # Link line must start with ### [ and contain properly escaped brackets and asterisks
    assert lines[0].startswith("### [")
    assert lines[0].endswith(f"]({uri})")
    assert r"\[Remix\]" in lines[0]
    assert r"\*cool\*" in lines[0]


def test_card_builder_mention_and_duration_format():
    from utils.components_v2 import build_nowplaying_container, Section, TextDisplay

    container = build_nowplaying_container(
        bot_name="Melora",
        title="Track Title",
        uri="https://example.com",
        requester_id=987654321,
        duration_str="3:42",
    )
    section = [c for c in container.children if isinstance(c, Section)][0]
    texts = [c for c in section.children if isinstance(c, TextDisplay)]
    lines = texts[1].content.splitlines()

    assert lines[0].startswith("### [")
    assert lines[1] == "**Requested by:** <@987654321>"
    assert lines[2] == "**Duration:** `3:42`"


def test_card_builder_button_order_and_styles():
    import discord
    from utils.components_v2 import ActionRow, Button, build_nowplaying_action_row

    row = build_nowplaying_action_row(is_paused=False, has_history=True)
    assert isinstance(row, ActionRow)
    buttons = [c for c in row.children if isinstance(c, Button)]
    assert len(buttons) == 5

    # Button order: Loop, Previous, Pause/Resume, Skip, Stop
    assert buttons[0].custom_id == "np:loop"
    assert buttons[1].custom_id == "np:previous"
    assert buttons[2].custom_id == "np:pause_resume"
    assert buttons[3].custom_id == "np:skip"
    assert buttons[4].custom_id == "np:stop"

    # Only Stop button is danger; all others are secondary
    assert buttons[0].style == discord.ButtonStyle.secondary
    assert buttons[1].style == discord.ButtonStyle.secondary
    assert buttons[2].style == discord.ButtonStyle.secondary
    assert buttons[3].style == discord.ButtonStyle.secondary
    assert buttons[4].style == discord.ButtonStyle.danger  # stop button is danger


def test_card_builder_emoji_swap_on_pause_and_resume():
    from utils.components_v2 import Button, build_nowplaying_action_row

    # Playing state -> Pause emoji
    playing_row = build_nowplaying_action_row(is_paused=False, has_history=True)
    btn_playing = [c for c in playing_row.children if isinstance(c, Button)][2]
    if btn_playing.emoji is not None:
        assert btn_playing.emoji.id == 1526024157526229082
    else:
        assert btn_playing.label == "Pause"

    # Paused state -> Resume emoji
    paused_row = build_nowplaying_action_row(is_paused=True, has_history=True)
    btn_paused = [c for c in paused_row.children if isinstance(c, Button)][2]
    if btn_paused.emoji is not None:
        assert btn_paused.emoji.id == 1526024227881353296
    else:
        assert btn_paused.label == "Resume"


def test_card_builder_previous_disabled_with_empty_history():
    from utils.components_v2 import Button, build_nowplaying_action_row

    # Empty history -> Previous disabled
    empty_row = build_nowplaying_action_row(is_paused=False, has_history=False)
    btn_empty = [c for c in empty_row.children if isinstance(c, Button)][1]
    assert btn_empty.disabled is True

    # Has history -> Previous enabled
    hist_row = build_nowplaying_action_row(is_paused=False, has_history=True)
    btn_hist = [c for c in hist_row.children if isinstance(c, Button)][1]
    assert btn_hist.disabled is False


def test_card_builder_no_divider_component():
    from utils.components_v2 import build_nowplaying_container

    container = build_nowplaying_container(
        bot_name="Melora",
        title="Song",
        uri="https://example.com",
        requester_id=42,
        duration_str="1:00",
        requester_avatar_url="https://example.com/avatar.png",
    )
    comp_dict = container.to_component_dict()

    def check_no_divider(d: dict):
        assert d.get("type") != 8
        for child in d.get("components", []):
            if isinstance(child, dict):
                check_no_divider(child)

    check_no_divider(comp_dict)


