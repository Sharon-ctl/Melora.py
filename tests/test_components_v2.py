from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import ButtonStyle

from utils.components_v2 import (
    BaseCardView,
    create_card_container,
    secondary_button,
)


def test_secondary_button():
    btn = secondary_button("Click me", custom_id="test_btn")
    assert btn.style == ButtonStyle.secondary
    assert btn.label == "Click me"
    assert btn.custom_id == "test_btn"
    assert not btn.disabled


def test_container_accent_colour():
    c = create_card_container()
    assert c.accent_colour == discord.Colour(0x4A4D53)


def test_no_separator_in_card_components():
    btn1 = secondary_button("<", disabled=True)
    btn2 = secondary_button(">")
    c = create_card_container(btn1, btn2)
    view = BaseCardView()
    view.add_item(c)
    raw = view.to_components()
    assert len(raw) == 1
    assert raw[0]["type"] == 17  # Container
    # Ensure no separator (type 11) is present
    for child in raw[0]["components"]:
        assert child.get("type") != 11


@pytest.mark.anyio
async def test_base_card_view_author_check():
    view = BaseCardView(author_id=123)

    # Valid author
    interaction_author = SimpleNamespace(user=SimpleNamespace(id=123), response=AsyncMock())
    assert await view.interaction_check(interaction_author) is True
    assert not interaction_author.response.send_message.called

    # Different author
    interaction_other = SimpleNamespace(user=SimpleNamespace(id=999), response=AsyncMock())
    assert await view.interaction_check(interaction_other) is False
    assert interaction_other.response.send_message.called


def test_disable_all_buttons():
    btn1 = secondary_button("1")
    btn2 = secondary_button("2")
    c = create_card_container(btn1, btn2)
    view = BaseCardView()
    view.add_item(c)

    assert not btn1.disabled
    assert not btn2.disabled

    view.disable_all_buttons()
    assert btn1.disabled
    assert btn2.disabled


@pytest.mark.anyio
async def test_paginated_view_rendering_and_bounds():
    from utils.components_v2 import PaginatedPage, PaginatedView

    def provider(page: int) -> PaginatedPage:
        # 25 total items across 3 pages
        start = (page - 1) * 10
        items = [f"Item {i + 1}" for i in range(start, min(start + 10, 25))]
        return PaginatedPage(
            title="**Test List**",
            items=items,
            current_page=page,
            total_pages=3,
        )

    view = PaginatedView(items_provider=provider, author_id=456, initial_page=1)
    await view.render(1)

    assert view._prev_btn is not None
    assert view._next_btn is not None
    assert view._prev_btn.disabled is True  # Page 1, prev disabled
    assert view._next_btn.disabled is False

    # Simulate next page
    await view.render(2)
    assert view._prev_btn.disabled is False
    assert view._next_btn.disabled is False

    # Simulate last page
    await view.render(3)
    assert view._prev_btn.disabled is False
    assert view._next_btn.disabled is True  # Page 3 of 3, next disabled

    # Timeout releases provider
    await view.on_timeout()
    assert view.items_provider is None
    assert view._prev_btn.disabled is True
    assert view._next_btn.disabled is True

