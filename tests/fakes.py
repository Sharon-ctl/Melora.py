"""Fake backend, audio player and loader used by the unit tests and the soak script."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from config import Config, load_config


def make_config(**overrides: Any) -> Config:
    env = {
        "DISCORD_TOKEN": "t" * 60,
        "OWNER_ID": "1",
        "LAVALINK_HOST": "127.0.0.1",
        "LAVALINK_PORT": "2333",
        "LAVALINK_PASSWORD": "test-password",
        "FAILURE_BREAKER": "3",
        "LOG_DIR": "logs",
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return load_config(env=env)


def make_track(n: int, duration_ms: int = 180_000, title: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        title=title or f"Track {n}",
        duration=duration_ms,
        is_stream=False,
        track=f"encoded-{n}",
    )


class FakeAudio:
    def __init__(self) -> None:
        self.paused = False
        self.current: Any = None
        self.volume = 100
        self.position = 0
        self.plays = 0
        self.fail_next = 0
        self.always_fail = False

    async def play(self, track: Any, *, volume: int | None = None, pause: bool | None = None) -> None:
        await asyncio.sleep(0)
        self.plays += 1
        if self.always_fail:
            raise RuntimeError("simulated play failure")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("simulated play failure")
        self.current = track
        self.position = 0

    async def stop(self) -> None:
        self.current = None
        self.position = 0

    async def set_pause(self, pause: bool) -> None:
        self.paused = pause

    async def set_volume(self, vol: int) -> None:
        self.volume = vol

    async def seek(self, position: int) -> None:
        await asyncio.sleep(0)
        self.position = position

    async def set_filter(self, _filter: Any, *, replace: bool = False) -> None:
        await asyncio.sleep(0)
        if not hasattr(self, "filters"):
            self.filters = {}
        if replace:
            self.filters.clear()
        name = getattr(_filter, "name", type(_filter).__name__.lower())
        self.filters[name] = _filter

    async def remove_filter(self, _filter: Any) -> None:
        await asyncio.sleep(0)
        if not hasattr(self, "filters"):
            self.filters = {}
        if isinstance(_filter, str):
            name = _filter.lower()
        elif isinstance(_filter, type):
            name = _filter.__name__.lower()
        else:
            name = getattr(_filter, "name", type(_filter).__name__.lower())
        self.filters.pop(name, None)

    async def clear_filters(self) -> None:
        await asyncio.sleep(0)
        if hasattr(self, "filters"):
            self.filters.clear()


class FakeBackend:
    def __init__(self) -> None:
        self.audios: dict[int, FakeAudio] = {}
        self.voice: dict[int, int] = {}
        self.humans: dict[int, int] = {}
        self.notices = 0
        self.nodes_up = True
        self.cards_sent: list[tuple[int, int]] = []
        self.cards_edited: list[tuple[int, int]] = []
        self.cards_deleted: list[tuple[int, int]] = []
        self.next_msg_id = 1000
        self.active_cards: dict[int, int] = {}
        self.fail_edit = False
        self.fail_send = False
        self.raise_not_found_on_edit = False

    def audio(self, guild_id: int) -> FakeAudio | None:
        return self.audios.get(guild_id)

    def voice_connected(self, guild_id: int, channel_id: int | None) -> bool:
        actual = self.voice.get(guild_id)
        return actual is not None and (channel_id is None or actual == channel_id)

    def humans_in_channel(self, guild_id: int, channel_id: int) -> int | None:
        return self.humans.get(guild_id, 1)

    def any_node_available(self) -> bool:
        return self.nodes_up

    def guild_ids(self) -> set[int]:
        return set(range(1, 10_000))

    def stray_guild_ids(self) -> set[int]:
        return set(self.audios) | set(self.voice)

    def bot_display_name(self, guild_id: int) -> str:
        return "Melora"

    def bot_avatar_url(self) -> str | None:
        return "https://cdn.discordapp.com/embed/avatars/0.png"

    def bot_id(self) -> int:
        return 999999

    async def connect(self, guild_id: int, channel_id: int) -> None:
        await asyncio.sleep(0)
        self.audios[guild_id] = FakeAudio()
        self.voice[guild_id] = channel_id

    async def purge(self, guild_id: int) -> None:
        self.audios.pop(guild_id, None)
        self.voice.pop(guild_id, None)

    async def notify(self, channel_id: int, text: str) -> None:
        self.notices += 1

    async def send_nowplaying_card(self, channel_id: int, container: Any, view: Any) -> int | None:
        await asyncio.sleep(0)
        if self.fail_send:
            return None
        self.next_msg_id += 1
        msg_id = self.next_msg_id
        self.active_cards[channel_id] = msg_id
        self.cards_sent.append((channel_id, msg_id))
        return msg_id

    async def edit_nowplaying_card(self, channel_id: int, message_id: int, container: Any, view: Any) -> bool:
        await asyncio.sleep(0)
        if self.raise_not_found_on_edit:
            import discord
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Card message not found")
        if self.fail_edit:
            return False
        self.cards_edited.append((channel_id, message_id))
        return True

    async def delete_nowplaying_card(self, channel_id: int, message_id: int) -> None:
        await asyncio.sleep(0)
        self.active_cards.pop(channel_id, None)
        self.cards_deleted.append((channel_id, message_id))


class FakeLoader:
    def __init__(self) -> None:
        self.fallback_calls = 0

    async def load_first_with_source(self, guild_id: int, source: str, text: str) -> Any:
        await asyncio.sleep(0)
        self.fallback_calls += 1
        return make_track(900_000 + self.fallback_calls, title=f"Fallback for {text}")

    def has_node(self) -> bool:
        return True

    async def search_candidates(self, text: str, limit: int = 25) -> list[tuple[str, str]]:
        await asyncio.sleep(0)
        return [(f"{text} result {i}", f"Artist {i}") for i in range(1, limit + 1)]

    async def load_with_source(self, guild_id: int, source: str, text: str) -> Any:
        await asyncio.sleep(0)
        self.fallback_calls += 1
        track = make_track(900_000 + self.fallback_calls, title=f"Track for {text}")
        return SimpleNamespace(tracks=[track], kind="search")

    async def load(self, guild_id: int, query: str) -> Any:
        await asyncio.sleep(0)
        self.fallback_calls += 1
        track = make_track(900_000 + self.fallback_calls, title=f"Track for {query}")
        return SimpleNamespace(tracks=[track], kind="search")

