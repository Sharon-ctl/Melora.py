"""Lavalink access: every call is async, time-limited, and classified.

Failures are mapped to three distinct user-facing errors:
no matches (NoMatches), load failure (LoadFailed), node offline (NodeOffline).
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import aiohttp
from lavalink.client import Client
from lavalink.errors import ClientError, RequestError
from lavalink.server import LoadResult, LoadType

from config import Config
from utils.errors import BotUserError, LoadFailed, NoMatches, NodeOffline
from utils.text import clean

log = logging.getLogger(__name__)

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
SUGGEST_TIMEOUT = 2.0


@dataclass(frozen=True)
class LoadOutcome:
    kind: str  # "track", "playlist" or "search"
    tracks: list[Any]
    playlist_name: str | None
    source: str
    used_fallback: bool
    query: str | None  # original search text, None for direct links


class LavalinkService:
    """Loads tracks with a global concurrency limit and one load per guild."""

    def __init__(self, client: Client, cfg: Config) -> None:
        self._client = client
        self._cfg = cfg
        self._semaphore = asyncio.Semaphore(cfg.max_concurrent_loads)
        self._slots: dict[int, list[Any]] = {}

    def has_node(self) -> bool:
        return bool(self._client.node_manager.available_nodes)

    @asynccontextmanager
    async def guild_slot(self, guild_id: int) -> AsyncIterator[None]:
        """Serialize loads per guild. The entry is removed when nobody uses it."""
        entry = self._slots.get(guild_id)
        if entry is None:
            entry = [asyncio.Lock(), 0]
            self._slots[guild_id] = entry
        entry[1] += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(entry[0].acquire(), timeout=3.0)
                acquired = True
            except (asyncio.TimeoutError, TimeoutError):
                from utils.errors import ServerBusy

                raise ServerBusy() from None
            yield
        finally:
            if acquired:
                entry[0].release()
            entry[1] -= 1
            if entry[1] <= 0 and self._slots.get(guild_id) is entry:
                del self._slots[guild_id]

    async def _fetch(self, identifier: str) -> LoadResult:
        if not self.has_node():
            raise NodeOffline()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=3.0)
        except (asyncio.TimeoutError, TimeoutError):
            from utils.errors import ServerBusy

            raise ServerBusy() from None
        try:
            return await asyncio.wait_for(self._client.get_tracks(identifier), timeout=self._cfg.load_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            raise LoadFailed("The audio server took too long to respond.") from None
        except ClientError:
            raise NodeOffline() from None
        except (aiohttp.ClientError, OSError):
            raise NodeOffline() from None
        except RequestError as exc:
            log.warning("Lavalink request error while loading: %s", exc)
            raise LoadFailed() from None
        except Exception:
            log.exception("Unexpected error while loading tracks")
            raise LoadFailed() from None
        finally:
            self._semaphore.release()

    def _to_outcome(self, result: LoadResult, source: str, used_fallback: bool, query: str | None) -> LoadOutcome:
        load_type = result.load_type
        if load_type == LoadType.ERROR:
            error = result.error
            log.warning("Lavalink load error: %s", error)
            raise LoadFailed()
        tracks = list(result.tracks)
        if load_type == LoadType.EMPTY or not tracks:
            raise NoMatches()
        if load_type == LoadType.PLAYLIST:
            name = clean(result.playlist_info.name) or "playlist"
            return LoadOutcome("playlist", tracks, name, source, used_fallback, query)
        if load_type == LoadType.SEARCH:
            return LoadOutcome("search", tracks, None, source, used_fallback, query)
        return LoadOutcome("track", tracks[:1], None, source, used_fallback, query)

    async def load(self, guild_id: int, query: str) -> LoadOutcome:
        """Resolve a link or search text.

        Direct links are loaded exactly as given. Search text uses the default
        source and, if that fails or finds nothing, retries once with the
        fallback source. The fallback never applies to direct links.
        """
        query = query.strip()
        if not query:
            raise NoMatches()
        budget = self._cfg.load_timeout * 2 + 5
        try:
            async with asyncio.timeout(budget):
                async with self.guild_slot(guild_id):
                    return await self._load_locked(query)
        except TimeoutError:
            raise LoadFailed("The search took too long. Try again.") from None

    async def _load_locked(self, query: str) -> LoadOutcome:
        if URL_RE.match(query):
            attempts = [(query, "direct", False, None)]
        else:
            attempts = [(f"{self._cfg.default_search_source}:{query}", self._cfg.default_search_source, False, query)]
            if self._cfg.fallback_search_source != self._cfg.default_search_source:
                attempts.append(
                    (f"{self._cfg.fallback_search_source}:{query}", self._cfg.fallback_search_source, True, query)
                )
        last_error: BotUserError | None = None
        for identifier, source, is_fallback, original in attempts:
            try:
                result = await self._fetch(identifier)
                return self._to_outcome(result, source, is_fallback, original)
            except NodeOffline:
                raise
            except BotUserError as exc:
                last_error = exc
                log.info("Load attempt with source %s failed: %s", source, exc.message)
        raise last_error or NoMatches()

    async def load_first_with_source(self, guild_id: int, source: str, text: str) -> Any:
        """Search one specific source and return the first track (used for start-failure fallback)."""
        try:
            async with asyncio.timeout(self._cfg.load_timeout * 2 + 5):
                async with self.guild_slot(guild_id):
                    result = await self._fetch(f"{source}:{text}")
                    outcome = self._to_outcome(result, source, True, text)
        except TimeoutError:
            raise LoadFailed("The search took too long.") from None
        return outcome.tracks[0]

    async def load_with_source(self, guild_id: int, source: str, text: str) -> LoadOutcome:
        """Search one specific source and return the outcome with all candidate tracks."""
        try:
            async with asyncio.timeout(self._cfg.load_timeout * 2 + 5):
                async with self.guild_slot(guild_id):
                    result = await self._fetch(f"{source}:{text}")
                    return self._to_outcome(result, source, False, text)
        except TimeoutError:
            raise LoadFailed("The search took too long.") from None

    async def suggest(self, text: str, limit: int = 10) -> list[str]:
        """Titles for autocomplete. Never raises, never waits for a busy server."""
        try:
            if not self.has_node() or self._semaphore.locked():
                return []
            async with self._semaphore:
                result = await asyncio.wait_for(
                    self._client.get_tracks(f"{self._cfg.default_search_source}:{text}"),
                    timeout=SUGGEST_TIMEOUT,
                )
            titles: list[str] = []
            for track in list(result.tracks)[:limit]:
                title = clean(getattr(track, "title", ""))
                if title and title not in titles:
                    titles.append(title)
            return titles
        except Exception:
            return []

    async def search_candidates(self, text: str, limit: int = 25) -> list[tuple[str, str]]:
        """Return (title, author) pairs for search autocomplete. Never raises."""
        try:
            if not self.has_node():
                return []
            async with self._semaphore:
                result = await asyncio.wait_for(
                    self._client.get_tracks(f"{self._cfg.default_search_source}:{text}"),
                    timeout=SUGGEST_TIMEOUT,
                )
            candidates: list[tuple[str, str]] = []
            seen: set[str] = set()
            for track in list(result.tracks):
                title = clean(getattr(track, "title", "") or "").strip()
                author = clean(getattr(track, "author", "") or "").strip()
                if not title:
                    continue
                key = f"{title.lower()}::{author.lower()}"
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((title, author))
                if len(candidates) >= limit:
                    break
            return candidates
        except Exception:
            return []
