"""Spotify metadata scraping and resolution without credentials or Spotify Web API.

Features:
- Swappable SpotifyProvider interface.
- Primary: spotifyscraper PyPI package executed in a thread pool.
- Fallback: lightweight aiohttp parser of public Spotify oEmbed and embed endpoints.
- Accurate reporting of "added N of M" tracks.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import aiohttp

from utils.cache import TTLCache

log = logging.getLogger(__name__)

SPOTIFY_URL_RE = re.compile(
    r"(?:https?://open\.spotify\.com/(?:[a-zA-Z0-9_-]+/)?(track|album|playlist)/([a-zA-Z0-9]+)|spotify:(track|album|playlist):([a-zA-Z0-9]+))"
)
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>')
REQUEST_TIMEOUT = 10.0


@dataclass(frozen=True)
class SpotifyTrack:
    title: str
    artists: list[str]
    duration_ms: int
    isrc: str | None = None
    uri: str = ""

    @property
    def artist_name(self) -> str:
        return ", ".join(self.artists) if self.artists else "Unknown Artist"

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else "Unknown Artist"


@dataclass(frozen=True)
class SpotifyAlbum:
    title: str
    artists: list[str]
    tracks: list[SpotifyTrack]
    total_tracks: int
    uri: str = ""


@dataclass(frozen=True)
class SpotifyPlaylist:
    title: str
    tracks: list[SpotifyTrack]
    total_tracks: int
    uri: str = ""


def parse_spotify_url(query: str) -> tuple[str, str] | None:
    """Extract (kind, spotify_id) from a Spotify URL or URI."""
    match = SPOTIFY_URL_RE.search(query.strip())
    if not match:
        return None
    kind = match.group(1) or match.group(3)
    ident = match.group(2) or match.group(4)
    if kind and ident:
        return kind.lower(), ident
    return None


class SpotifyProvider(ABC):
    """Abstract interface for scraping Spotify track, album, and playlist metadata."""

    @abstractmethod
    async def get_track(self, spotify_id: str) -> SpotifyTrack | None:
        """Fetch track metadata."""

    @abstractmethod
    async def get_album(self, spotify_id: str) -> SpotifyAlbum | None:
        """Fetch album metadata."""

    @abstractmethod
    async def get_playlist(self, spotify_id: str) -> SpotifyPlaylist | None:
        """Fetch playlist metadata."""


class ScraperSpotifyProvider(SpotifyProvider):
    """Primary provider using the spotifyscraper (spotify_scraper) library."""

    def __init__(self) -> None:
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            import spotify_scraper

            self._client = spotify_scraper.SpotifyClient()
        return self._client

    async def get_track(self, spotify_id: str) -> SpotifyTrack | None:
        try:
            client = self._get_client()
            track_obj = await asyncio.wait_for(
                asyncio.to_thread(client.get_track, spotify_id),
                timeout=REQUEST_TIMEOUT,
            )
            if not track_obj or not getattr(track_obj, "name", None):
                return None
            artists = [a.name for a in getattr(track_obj, "artists", []) if getattr(a, "name", None)]
            return SpotifyTrack(
                title=str(track_obj.name),
                artists=artists,
                duration_ms=int(getattr(track_obj, "duration_ms", 0) or 0),
                isrc=getattr(track_obj, "isrc", None),
                uri=f"spotify:track:{spotify_id}",
            )
        except Exception as exc:
            log.debug("ScraperSpotifyProvider get_track(%s) failed: %s", spotify_id, exc)
            return None

    async def get_album(self, spotify_id: str) -> SpotifyAlbum | None:
        try:
            client = self._get_client()
            album_obj = await asyncio.wait_for(
                asyncio.to_thread(client.get_album, spotify_id),
                timeout=REQUEST_TIMEOUT,
            )
            if not album_obj or not getattr(album_obj, "name", None):
                return None
            album_artists = [a.name for a in getattr(album_obj, "artists", []) if getattr(a, "name", None)]
            raw_tracks = getattr(album_obj, "tracks", []) or []
            tracks: list[SpotifyTrack] = []
            for t in raw_tracks:
                if not getattr(t, "name", None):
                    continue
                t_artists = [a.name for a in getattr(t, "artists", []) if getattr(a, "name", None)] or album_artists
                tracks.append(
                    SpotifyTrack(
                        title=str(t.name),
                        artists=t_artists,
                        duration_ms=int(getattr(t, "duration_ms", 0) or 0),
                        isrc=getattr(t, "isrc", None),
                        uri=f"spotify:track:{getattr(t, 'id', '')}",
                    )
                )
            total = int(getattr(album_obj, "total_tracks", 0) or len(tracks))
            return SpotifyAlbum(
                title=str(album_obj.name),
                artists=album_artists,
                tracks=tracks,
                total_tracks=max(total, len(tracks)),
                uri=f"spotify:album:{spotify_id}",
            )
        except Exception as exc:
            log.debug("ScraperSpotifyProvider get_album(%s) failed: %s", spotify_id, exc)
            return None

    async def get_playlist(self, spotify_id: str) -> SpotifyPlaylist | None:
        try:
            client = self._get_client()
            playlist_obj = await asyncio.wait_for(
                asyncio.to_thread(client.get_playlist, spotify_id, max_tracks=None),
                timeout=REQUEST_TIMEOUT,
            )
            if not playlist_obj or not getattr(playlist_obj, "name", None):
                return None
            raw_tracks = getattr(playlist_obj, "tracks", []) or []
            tracks: list[SpotifyTrack] = []
            for item in raw_tracks:
                t = getattr(item, "track", item)
                if not t or not getattr(t, "name", None):
                    continue
                t_artists = [a.name for a in getattr(t, "artists", []) if getattr(a, "name", None)]
                tracks.append(
                    SpotifyTrack(
                        title=str(t.name),
                        artists=t_artists,
                        duration_ms=int(getattr(t, "duration_ms", 0) or 0),
                        isrc=getattr(t, "isrc", None),
                        uri=f"spotify:track:{getattr(t, 'id', '')}",
                    )
                )
            total = int(getattr(playlist_obj, "total_tracks", 0) or len(tracks))
            return SpotifyPlaylist(
                title=str(playlist_obj.name),
                tracks=tracks,
                total_tracks=max(total, len(tracks)),
                uri=f"spotify:playlist:{spotify_id}",
            )
        except Exception as exc:
            log.debug("ScraperSpotifyProvider get_playlist(%s) failed: %s", spotify_id, exc)
            return None


class EmbedSpotifyProvider(SpotifyProvider):
    """Fallback provider scraping Spotify public embed page and oEmbed endpoint via aiohttp."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                    )
                }
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _fetch_oembed(self, url: str) -> dict[str, Any] | None:
        session = await self._get_session()
        try:
            async with session.get(
                "https://open.spotify.com/oembed",
                params={"url": url},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as exc:
            log.debug("oEmbed fetch failed for %s: %s", url, exc)
        return None

    async def _fetch_embed_html(self, kind: str, spotify_id: str) -> str | None:
        session = await self._get_session()
        try:
            url = f"https://open.spotify.com/embed/{kind}/{spotify_id}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                if resp.status == 200:
                    return await resp.text()
        except Exception as exc:
            log.debug("Embed html fetch failed for %s/%s: %s", kind, spotify_id, exc)
        return None

    async def get_track(self, spotify_id: str) -> SpotifyTrack | None:
        url = f"https://open.spotify.com/track/{spotify_id}"
        # 1. Try oEmbed
        data = await self._fetch_oembed(url)
        if data and "title" in data:
            title_str = html.unescape(data["title"])
            # Format is often "Track Title - Artist Name" or "Track Title"
            artists: list[str] = []
            if " - " in title_str:
                parts = title_str.split(" - ")
                title = parts[0].strip()
                artists = [p.strip() for p in parts[1:]]
            else:
                title = title_str
            return SpotifyTrack(
                title=title,
                artists=artists,
                duration_ms=0,
                uri=f"spotify:track:{spotify_id}",
            )

        # 2. Try embed HTML
        html_text = await self._fetch_embed_html("track", spotify_id)
        if not html_text:
            return None

        # Look for __NEXT_DATA__ JSON
        next_data_match = _NEXT_DATA_RE.search(html_text)
        if next_data_match:
            try:
                parsed = json.loads(next_data_match.group(1))
                entity = (
                    parsed.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                )
                if entity and entity.get("name"):
                    artists = [a.get("name") for a in entity.get("artists", []) if a.get("name")]
                    dur = int(entity.get("duration", 0) or 0)
                    return SpotifyTrack(
                        title=entity["name"],
                        artists=artists,
                        duration_ms=dur,
                        uri=f"spotify:track:{spotify_id}",
                    )
            except Exception as exc:
                log.debug("Failed parsing __NEXT_DATA__ in embed: %s", exc)

        return None

    async def get_album(self, spotify_id: str) -> SpotifyAlbum | None:
        html_text = await self._fetch_embed_html("album", spotify_id)
        if not html_text:
            return None

        tracks: list[SpotifyTrack] = []
        album_name = f"Album {spotify_id}"
        artists: list[str] = []
        total = 0

        next_data_match = _NEXT_DATA_RE.search(html_text)
        if next_data_match:
            try:
                parsed = json.loads(next_data_match.group(1))
                entity = (
                    parsed.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                )
                if entity:
                    album_name = entity.get("name", album_name)
                    artists = [a.get("name") for a in entity.get("artists", []) if a.get("name")]
                    track_list = entity.get("trackList", [])
                    total = int(entity.get("total_tracks", len(track_list)) or len(track_list))
                    for t in track_list:
                        name = t.get("title") or t.get("name")
                        if not name:
                            continue
                        t_artists = [t.get("subtitle")] if t.get("subtitle") else artists
                        dur = int(t.get("duration", 0) or 0)
                        uri = t.get("uri", "")
                        tracks.append(
                            SpotifyTrack(
                                title=name,
                                artists=t_artists,
                                duration_ms=dur,
                                uri=uri or f"spotify:track:{t.get('id', '')}",
                            )
                        )
            except Exception as exc:
                log.debug("Failed parsing album __NEXT_DATA__: %s", exc)

        if not tracks:
            # Fallback to oembed for title
            oembed_data = await self._fetch_oembed(f"https://open.spotify.com/album/{spotify_id}")
            if oembed_data and "title" in oembed_data:
                album_name = oembed_data["title"]

        return SpotifyAlbum(
            title=album_name,
            artists=artists,
            tracks=tracks,
            total_tracks=max(total, len(tracks)),
            uri=f"spotify:album:{spotify_id}",
        )

    async def get_playlist(self, spotify_id: str) -> SpotifyPlaylist | None:
        html_text = await self._fetch_embed_html("playlist", spotify_id)
        if not html_text:
            return None

        tracks: list[SpotifyTrack] = []
        playlist_name = f"Playlist {spotify_id}"
        total = 0

        next_data_match = _NEXT_DATA_RE.search(html_text)
        if next_data_match:
            try:
                parsed = json.loads(next_data_match.group(1))
                entity = (
                    parsed.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                )
                if entity:
                    playlist_name = entity.get("name", playlist_name)
                    track_list = entity.get("trackList", [])
                    total = int(entity.get("total_tracks", len(track_list)) or len(track_list))
                    for t in track_list:
                        name = t.get("title") or t.get("name")
                        if not name:
                            continue
                        t_artists = [t.get("subtitle")] if t.get("subtitle") else []
                        dur = int(t.get("duration", 0) or 0)
                        uri = t.get("uri", "")
                        tracks.append(
                            SpotifyTrack(
                                title=name,
                                artists=t_artists,
                                duration_ms=dur,
                                uri=uri or f"spotify:track:{t.get('id', '')}",
                            )
                        )
            except Exception as exc:
                log.debug("Failed parsing playlist __NEXT_DATA__: %s", exc)

        if not tracks:
            oembed_data = await self._fetch_oembed(f"https://open.spotify.com/playlist/{spotify_id}")
            if oembed_data and "title" in oembed_data:
                playlist_name = oembed_data["title"]

        return SpotifyPlaylist(
            title=playlist_name,
            tracks=tracks,
            total_tracks=max(total, len(tracks)),
            uri=f"spotify:playlist:{spotify_id}",
        )


class SpotifyService:
    """Unified Spotify service with swappable primary and fallback providers."""

    def __init__(
        self,
        enabled: bool = True,
        primary: SpotifyProvider | None = None,
        fallback: SpotifyProvider | None = None,
    ) -> None:
        self.enabled = enabled
        self.primary = primary or ScraperSpotifyProvider()
        self.fallback = fallback or EmbedSpotifyProvider()
        self._track_cache: TTLCache[str, SpotifyTrack] = TTLCache(max_size=1000, ttl=3600.0)
        self._album_cache: TTLCache[str, SpotifyAlbum] = TTLCache(max_size=200, ttl=3600.0)
        self._playlist_cache: TTLCache[str, SpotifyPlaylist] = TTLCache(max_size=200, ttl=1800.0)

    async def close(self) -> None:
        if isinstance(self.fallback, EmbedSpotifyProvider):
            await self.fallback.close()

    async def get_track(self, spotify_id: str) -> SpotifyTrack | None:
        if not self.enabled:
            return None
        cached = self._track_cache.get(spotify_id)
        if cached is not None:
            return cached
        res = await self.primary.get_track(spotify_id)
        if res is not None:
            self._track_cache.set(spotify_id, res)
            return res
        res = await self.fallback.get_track(spotify_id)
        if res is not None:
            self._track_cache.set(spotify_id, res)
        return res

    async def get_album(self, spotify_id: str) -> SpotifyAlbum | None:
        if not self.enabled:
            return None
        cached = self._album_cache.get(spotify_id)
        if cached is not None:
            return cached
        res = await self.primary.get_album(spotify_id)
        if res is not None:
            self._album_cache.set(spotify_id, res)
            return res
        res = await self.fallback.get_album(spotify_id)
        if res is not None:
            self._album_cache.set(spotify_id, res)
        return res

    async def get_playlist(self, spotify_id: str) -> SpotifyPlaylist | None:
        if not self.enabled:
            return None
        cached = self._playlist_cache.get(spotify_id)
        if cached is not None:
            return cached
        res = await self.primary.get_playlist(spotify_id)
        if res is not None:
            self._playlist_cache.set(spotify_id, res)
            return res
        res = await self.fallback.get_playlist(spotify_id)
        if res is not None:
            self._playlist_cache.set(spotify_id, res)
        return res
