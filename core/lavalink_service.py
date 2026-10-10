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
from utils.cache import TTLCache
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
        self._per_guild_concurrency = getattr(cfg, "per_guild_concurrent_loads", 4)
        self._slots: dict[int, list[Any]] = {}
        self._cache: TTLCache[str, LoadResult] = TTLCache(max_size=2000, ttl=300.0)
        self._negative_cache: TTLCache[str, Exception] = TTLCache(max_size=1000, ttl=30.0)
        self._flight: dict[str, asyncio.Future[LoadResult]] = {}

    def has_node(self) -> bool:
        return bool(self._client.node_manager.available_nodes)

    @asynccontextmanager
    async def guild_slot(self, guild_id: int) -> AsyncIterator[None]:
        """Serialize loads per guild with configurable concurrency. The entry is removed when nobody uses it."""
        entry = self._slots.get(guild_id)
        if entry is None:
            entry = [asyncio.Semaphore(self._per_guild_concurrency), 0]
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

        # Check negative cache (30-second TTL)
        neg_exc = self._negative_cache.get(identifier)
        if neg_exc is not None:
            raise neg_exc

        # Check positive cache
        cached = self._cache.get(identifier)
        if cached is not None:
            return cached

        # Single-flight deduplication: reuse in-flight future if identical load is pending
        fut = self._flight.get(identifier)
        if fut is not None:
            return await fut

        loop = asyncio.get_running_loop()
        flight_fut: asyncio.Future[LoadResult] = loop.create_future()

        def _consume_flight_exception(f: asyncio.Future[Any]) -> None:
            if not f.cancelled():
                f.exception()

        flight_fut.add_done_callback(_consume_flight_exception)
        self._flight[identifier] = flight_fut

        source_name = identifier.split(":", 1)[0] if ":" in identifier else "direct"

        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=3.0)
            except (asyncio.TimeoutError, TimeoutError):
                from utils.errors import ServerBusy

                raise ServerBusy() from None
            try:
                result = await asyncio.wait_for(self._client.get_tracks(identifier), timeout=self._cfg.load_timeout)
                # Check for load failure or empty tracks to cache negatively
                if result.load_type == LoadType.ERROR:
                    err = getattr(result, "error", None)
                    err_msg = getattr(err, "message", None) if err else None
                    err_sev = getattr(err, "severity", None) if err else None
                    sev_str = str(err_sev) if err_sev is not None else None
                    exc = LoadFailed(
                        cause=err_msg or "Lavalink load error",
                        severity=sev_str,
                        source=source_name,
                    )
                    self._negative_cache.set(identifier, exc)
                    if not flight_fut.done():
                        flight_fut.set_exception(exc)
                    raise exc
                if result.load_type == LoadType.EMPTY or not result.tracks:
                    exc = NoMatches(
                        cause="No tracks found",
                        severity="COMMON",
                        source=source_name,
                    )
                    self._negative_cache.set(identifier, exc)
                    if not flight_fut.done():
                        flight_fut.set_exception(exc)
                    raise exc

                # Cache positive result
                self._cache.set(identifier, result)
                if not flight_fut.done():
                    flight_fut.set_result(result)
                return result
            except (asyncio.TimeoutError, TimeoutError):
                exc = LoadFailed(
                    "The audio server took too long to respond.",
                    cause="Audio server timeout",
                    severity="COMMON",
                    source=source_name,
                )
                self._negative_cache.set(identifier, exc)
                if not flight_fut.done():
                    flight_fut.set_exception(exc)
                raise exc from None
            except ClientError:
                exc = NodeOffline()
                if not flight_fut.done():
                    flight_fut.set_exception(exc)
                raise exc from None
            except (aiohttp.ClientError, OSError):
                exc = NodeOffline()
                if not flight_fut.done():
                    flight_fut.set_exception(exc)
                raise exc from None
            except RequestError as req_exc:
                log.warning("Lavalink request error while loading: %s", req_exc)
                exc = LoadFailed(cause=str(req_exc), severity="FAULT", source=source_name)
                self._negative_cache.set(identifier, exc)
                if not flight_fut.done():
                    flight_fut.set_exception(exc)
                raise exc from None
            except (NoMatches, LoadFailed, NodeOffline):
                raise
            except Exception as exc_any:
                log.exception("Unexpected error while loading tracks")
                exc = LoadFailed(cause=str(exc_any), severity="FAULT", source=source_name)
                if not flight_fut.done():
                    flight_fut.set_exception(exc)
                raise exc from None
            finally:
                self._semaphore.release()
        except (asyncio.CancelledError, GeneratorExit):
            if not flight_fut.done():
                flight_fut.cancel()
            raise
        except Exception as e:
            if not flight_fut.done():
                flight_fut.set_exception(e)
            raise
        finally:
            self._flight.pop(identifier, None)

    def _to_outcome(self, result: LoadResult, source: str, used_fallback: bool, query: str | None) -> LoadOutcome:
        load_type = result.load_type
        if load_type == LoadType.ERROR:
            error = getattr(result, "error", None)
            err_msg = getattr(error, "message", None) if error else None
            err_sev = getattr(error, "severity", None) if error else None
            sev_str = str(err_sev) if err_sev is not None else None
            log.warning("Lavalink load error with source %s: message=%s severity=%s", source, err_msg, sev_str)
            raise LoadFailed(cause=err_msg or "Lavalink load error", severity=sev_str, source=source)
        tracks = list(result.tracks)
        if load_type == LoadType.EMPTY or not tracks:
            raise NoMatches(cause="No tracks found", severity="COMMON", source=source)
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
                if getattr(exc, "source", None) is None:
                    exc.source = source
                desc = "no matches found" if isinstance(exc, NoMatches) else "track loading failed"
                details = []
                if getattr(exc, "severity", None):
                    details.append(f"severity={exc.severity}")
                if getattr(exc, "cause", None):
                    details.append(f"cause={exc.cause}")
                details_str = f" ({', '.join(details)})" if details else ""
                log.info("Load attempt with source %s failed: %s: %s%s", source, type(exc).__name__, desc, details_str)
        raise last_error or NoMatches(source=attempts[-1][1] if attempts else "unknown")

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
