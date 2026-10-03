from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.admin import HelpView
from config import load_config
from core.alerts import Alerter
from main import MusicBot


def make_bot_and_admin():
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
    admin_cog = bot.get_cog("Admin")
    return bot, admin_cog


@pytest.mark.anyio
async def test_help_view_overview():
    categories = ["Overview", "Playback", "Queue", "Info"]
    descriptions = {
        "Overview": "Bot overview and tips",
        "Playback": "Audio controls",
        "Queue": "Queue management",
        "Info": "Bot information",
    }
    commands_by_cat = {
        "Playback": [f"`/play_{i}` - description" for i in range(15)],
        "Queue": ["`/queue` - view queue"],
    }
    view = HelpView(
        categories=categories,
        descriptions=descriptions,
        commands_by_cat=commands_by_cat,
        author_id=123,
    )
    await view.render()

    assert view.selected_category == "Overview"
    assert view.total_pages == 1
    assert view.current_page == 1
    assert view._prev_btn is not None and view._prev_btn.disabled is True
    assert view._next_btn is not None and view._next_btn.disabled is True

    # Check select options
    assert view._select is not None
    assert len(view._select.options) == 4
    default_opts = [opt for opt in view._select.options if opt.default]
    assert len(default_opts) == 1
    assert default_opts[0].value == "Overview"


@pytest.mark.anyio
async def test_help_view_category_switch_and_pagination():
    categories = ["Overview", "Playback", "Queue"]
    descriptions = {"Playback": "Audio controls"}
    # 25 commands in Playback -> 3 pages of 10
    commands_by_cat = {
        "Playback": [f"`/play_{i}` - desc {i}" for i in range(25)],
    }
    view = HelpView(
        categories=categories,
        descriptions=descriptions,
        commands_by_cat=commands_by_cat,
        author_id=123,
    )
    await view.render()

    # Switch to Playback
    view.selected_category = "Playback"
    view.current_page = 1
    await view.render()

    assert view.total_pages == 3
    assert view.current_page == 1
    assert view._prev_btn.disabled is True
    assert view._next_btn.disabled is False

    # Simulate next page
    interaction_author = SimpleNamespace(user=SimpleNamespace(id=123), response=AsyncMock())
    await view._on_next(interaction_author)
    assert view.current_page == 2
    assert view._prev_btn.disabled is False
    assert view._next_btn.disabled is False

    # Simulate next page to page 3
    await view._on_next(interaction_author)
    assert view.current_page == 3
    assert view._prev_btn.disabled is False
    assert view._next_btn.disabled is True

    # Unauthorized user gets rejection
    interaction_stranger = SimpleNamespace(user=SimpleNamespace(id=999), response=AsyncMock())
    await view._on_prev(interaction_stranger)
    assert interaction_stranger.response.send_message.called
    assert view.current_page == 3  # unchanged
