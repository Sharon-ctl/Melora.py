"""Rate-limited autocomplete must answer with an empty list using a real discord.py method."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord

from tests.fakes import make_config


def test_rate_limited_autocomplete_responds_with_empty_choices():
    from core.alerts import Alerter
    from main import MusicBot

    async def scenario():
        cfg = make_config()
        bot = MusicBot(cfg, Alerter(cfg))
        bot.rate_limiter.acquire_autocomplete = MagicMock(return_value=(False, 1.0))

        inter = MagicMock(spec=discord.Interaction)
        inter.extras = {}
        inter.type = discord.InteractionType.autocomplete
        inter.user = MagicMock()
        inter.user.id = cfg.owner_id + 1
        # spec= makes a call to a method discord.py does not have raise AttributeError.
        inter.response = MagicMock(spec=discord.InteractionResponse)
        inter.response.autocomplete = AsyncMock()

        allowed = await bot._tree_interaction_check(inter)

        assert allowed is False
        inter.response.autocomplete.assert_awaited_once_with([])

    asyncio.run(scenario())
