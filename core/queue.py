"""The per-guild FIFO queue with loop modes, history, and limits.

Pure data structure: no Discord or Lavalink imports beyond error types.
"""
from __future__ import annotations

import random
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum
from itertools import islice
from math import ceil
from typing import Any

from utils.cache import TTLCache
from utils.errors import QueueFull, TrackTooLong, UserLimitReached
from utils.text import clean, format_duration

MAX_TITLE_CHARS = 200
DEFAULT_HISTORY_SIZE = 50

_AVATAR_CACHE: TTLCache[int, str] = TTLCache(max_size=10000, ttl=86400.0)


def clear_avatar_cache() -> None:
    _AVATAR_CACHE.clear()


class LoopMode(str, Enum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


def _title_of(track: Any) -> str:
    title = clean(getattr(track, "title", "") or "")
    return (title or "Unknown title")[:MAX_TITLE_CHARS]


def _artist_of(track: Any) -> str:
    author = clean(getattr(track, "author", "") or "")
    return author[:100]


def _uri_of(track: Any) -> str:
    return str(getattr(track, "uri", "") or "")[:500]


class QueueItem:
    """One queued track. Uses __slots__ and essential fields only."""

    __slots__ = (
        "track",
        "title",
        "duration_ms",
        "requester_id",
        "is_stream",
        "query",
        "fallback_used",
        "artist",
        "uri",
        "isrc",
        "spotify_metadata",
        "requester_name",
        "artwork_url",
    )

    def __init__(
        self,
        track: Any,
        title: str,
        duration_ms: int,
        requester_id: int,
        is_stream: bool = False,
        query: str | None = None,
        fallback_used: bool = False,
        artist: str = "",
        uri: str = "",
        isrc: str | None = None,
        spotify_metadata: dict[str, Any] | None = None,
        requester_name: str = "",
        artwork_url: str | None = None,
        requester_avatar_url: str | None = None,
    ) -> None:
        self.track = track
        self.title = title
        self.duration_ms = duration_ms
        self.requester_id = requester_id
        self.is_stream = is_stream
        self.query = query
        self.fallback_used = fallback_used
        self.artist = artist
        self.uri = uri
        self.isrc = isrc
        self.spotify_metadata = spotify_metadata
        self.requester_name = requester_name
        self.artwork_url = artwork_url
        if requester_avatar_url:
            _AVATAR_CACHE.set(requester_id, requester_avatar_url)

    def __repr__(self) -> str:
        return (
            f"QueueItem(title={self.title!r}, artist={self.artist!r}, "
            f"duration_ms={self.duration_ms}, requester_id={self.requester_id})"
        )

    @property
    def requester_avatar_url(self) -> str | None:
        return _AVATAR_CACHE.get(self.requester_id)

    @requester_avatar_url.setter
    def requester_avatar_url(self, url: str | None) -> None:
        if url:
            _AVATAR_CACHE.set(self.requester_id, url)

    @classmethod
    def from_track(
        cls,
        track: Any,
        requester_id: int,
        *,
        query: str | None = None,
        fallback_used: bool = False,
        requester_name: str = "",
        artwork_url: str | None = None,
        requester_avatar_url: str | None = None,
    ) -> QueueItem:
        art = getattr(track, "artwork_url", None) or getattr(track, "artworkUrl", None) or artwork_url
        return cls(
            track=track,
            title=_title_of(track),
            duration_ms=int(getattr(track, "duration", 0) or 0),
            requester_id=requester_id,
            is_stream=bool(getattr(track, "is_stream", False)),
            query=query,
            fallback_used=fallback_used,
            artist=_artist_of(track),
            uri=_uri_of(track),
            requester_name=requester_name,
            artwork_url=art,
            requester_avatar_url=requester_avatar_url,
        )

    @classmethod
    def from_spotify(
        cls,
        track: Any,
        requester_id: int,
        *,
        requester_name: str = "",
        artwork_url: str | None = None,
        requester_avatar_url: str | None = None,
    ) -> QueueItem:
        artists = getattr(track, "artists", [])
        artist_name = ", ".join(artists) if artists else getattr(track, "artist_name", "")
        title = getattr(track, "title", "Unknown")
        dur = int(getattr(track, "duration_ms", 0) or 0)
        uri = getattr(track, "uri", "")
        isrc = getattr(track, "isrc", None)
        art = getattr(track, "artwork_url", None) or artwork_url
        return cls(
            track=None,
            title=title,
            duration_ms=dur,
            requester_id=requester_id,
            is_stream=False,
            artist=artist_name,
            uri=uri,
            isrc=isrc,
            spotify_metadata={
                "title": title,
                "artists": list(artists),
                "duration_ms": dur,
                "isrc": isrc,
                "uri": uri,
            },
            requester_name=requester_name,
            artwork_url=art,
            requester_avatar_url=requester_avatar_url,
        )

    def replace_track(self, track: Any) -> None:
        """Swap in a different track (used by search fallback and lazy Spotify resolution)."""
        self.track = track
        self.title = _title_of(track)
        self.duration_ms = int(getattr(track, "duration", 0) or 0)
        self.is_stream = bool(getattr(track, "is_stream", False))
        self.artist = _artist_of(track) or self.artist
        self.uri = _uri_of(track) or self.uri
        self.spotify_metadata = None
        art = getattr(track, "artwork_url", None) or getattr(track, "artworkUrl", None)
        if art:
            self.artwork_url = art


def validate_track(track: Any, max_seconds: int) -> None:
    """Raise TrackTooLong if the track is a live stream or exceeds the limit."""
    if bool(getattr(track, "is_stream", False)):
        raise TrackTooLong("Live streams are not supported.")
    if max_seconds > 0:
        duration = int(getattr(track, "duration", 0) or 0)
        if duration > max_seconds * 1000:
            raise TrackTooLong(f"That track is longer than the limit of {format_duration(max_seconds * 1000)}.")


@dataclass(frozen=True)
class AddResult:
    added: int
    skipped: int
    reason: str | None


class TrackQueue:
    """FIFO queue of upcoming tracks with bounded history and position operations."""

    def __init__(self, max_size: int = 0, max_per_user: int = 0, history_size: int = DEFAULT_HISTORY_SIZE) -> None:
        if max_size < 0 or max_per_user < 0:
            raise ValueError("max_size and max_per_user must be at least 0")
        self.max_size = max_size
        self.max_per_user = max_per_user
        self.history_size = max(1, history_size)
        self.loop = LoopMode.OFF
        self._items: deque[QueueItem] = deque()
        self._counts: dict[int, int] = {}
        self._history: deque[QueueItem] = deque()

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[QueueItem]:
        return iter(self._items)

    def __getitem__(self, index: int) -> QueueItem:
        return self._items[index]

    def count_for(self, user_id: int) -> int:
        return self._counts.get(user_id, 0)

    def can_add(self, user_id: int) -> str | None:
        """Return None if a track can be added, else 'full' or 'user'."""
        if self.max_size > 0 and len(self._items) >= self.max_size:
            return "full"
        if self.max_per_user > 0 and self.count_for(user_id) >= self.max_per_user:
            return "user"
        return None

    def _push(self, item: QueueItem) -> None:
        self._items.append(item)
        self._counts[item.requester_id] = self._counts.get(item.requester_id, 0) + 1

    def _release(self, item: QueueItem) -> None:
        remaining = self._counts.get(item.requester_id, 0) - 1
        if remaining > 0:
            self._counts[item.requester_id] = remaining
        else:
            self._counts.pop(item.requester_id, None)

    def add(self, item: QueueItem) -> None:
        reason = self.can_add(item.requester_id)
        if reason == "full":
            raise QueueFull()
        if reason == "user":
            raise UserLimitReached(self.max_per_user)
        self._push(item)

    def add_many(self, items: Iterable[QueueItem]) -> AddResult:
        """Add as many items as the limits allow and report what was skipped."""
        added = 0
        skipped = 0
        reason: str | None = None
        for item in items:
            why = self.can_add(item.requester_id)
            if why is None:
                self._push(item)
                added += 1
            else:
                skipped += 1
                reason = reason or why
        return AddResult(added, skipped, reason)

    def insert(self, position: int, item: QueueItem) -> int:
        """Insert track at 1-based position. Returns the 1-based position where placed."""
        reason = self.can_add(item.requester_id)
        if reason == "full":
            raise QueueFull()
        if reason == "user":
            raise UserLimitReached(self.max_per_user)

        target_idx = max(0, min(position - 1, len(self._items)))
        self._items.insert(target_idx, item)
        self._counts[item.requester_id] = self._counts.get(item.requester_id, 0) + 1
        return target_idx + 1

    def push_front(self, item: QueueItem) -> None:
        """Push a track back to the very front of the upcoming queue (used by /previous)."""
        self._items.appendleft(item)
        self._counts[item.requester_id] = self._counts.get(item.requester_id, 0) + 1

    def next_item(self, current: QueueItem | None, *, skipped: bool = False, failed: bool = False) -> QueueItem | None:
        """Pick what plays after current according to the loop mode.

        - track loop repeats current unless it was skipped or failed
        - queue loop sends current to the back unless it failed
        - a failed track is never replayed or saved to history
        """
        if current is not None and not failed:
            self.record_history(current)
            if self.loop is LoopMode.TRACK and not skipped:
                return current
            if self.loop is LoopMode.QUEUE:
                self._push(current)
        if not self._items:
            return None
        item = self._items.popleft()
        self._release(item)
        return item

    def remove_many(self, position: int, count: int = 1) -> list[QueueItem]:
        """Remove count tracks starting from 1-based position. Raises IndexError if position invalid."""
        total = len(self._items)
        if position < 1 or position > total:
            raise IndexError(position)
        actual_count = max(1, min(count, total - position + 1))
        removed: list[QueueItem] = []
        if actual_count <= 4:
            for _ in range(actual_count):
                item = self._items[position - 1]
                del self._items[position - 1]
                self._release(item)
                removed.append(item)
        else:
            items_list = list(self._items)
            start_idx = position - 1
            end_idx = start_idx + actual_count
            removed = items_list[start_idx:end_idx]
            for item in removed:
                self._release(item)
            del items_list[start_idx:end_idx]
            self._items = deque(items_list)
        return removed

    def remove(self, position: int) -> QueueItem:
        """Remove the track at a 1-based position. Raises IndexError if invalid."""
        return self.remove_many(position, 1)[0]

    def move(self, from_pos: int, to_pos: int) -> QueueItem:
        """Move a track from 1-based from_pos to to_pos. Raises IndexError if invalid."""
        total = len(self._items)
        if from_pos < 1 or from_pos > total:
            raise IndexError(from_pos)
        if to_pos < 1 or to_pos > total:
            raise IndexError(to_pos)
        if from_pos == to_pos:
            return self._items[from_pos - 1]
        item = self._items[from_pos - 1]
        del self._items[from_pos - 1]
        self._items.insert(to_pos - 1, item)
        return item

    def swap(self, pos1: int, pos2: int) -> tuple[QueueItem, QueueItem]:
        """Swap two tracks at 1-based positions. Raises IndexError if invalid."""
        total = len(self._items)
        if pos1 < 1 or pos1 > total:
            raise IndexError(pos1)
        if pos2 < 1 or pos2 > total:
            raise IndexError(pos2)
        idx1, idx2 = pos1 - 1, pos2 - 1
        self._items[idx1], self._items[idx2] = self._items[idx2], self._items[idx1]
        return self._items[idx1], self._items[idx2]

    def skipto(self, position: int) -> list[QueueItem]:
        """Drop all upcoming tracks before 1-based position. Raises IndexError if invalid."""
        total = len(self._items)
        if position < 1 or position > total:
            raise IndexError(position)
        dropped_count = position - 1
        if dropped_count <= 0:
            return []
        dropped: list[QueueItem] = []
        for _ in range(dropped_count):
            item = self._items.popleft()
            self._release(item)
            dropped.append(item)
        return dropped

    def dedupe(self) -> int:
        """Remove duplicate tracks in upcoming queue, keeping first occurrence. Returns count removed."""
        seen: set[str] = set()
        unique_items: deque[QueueItem] = deque()
        removed_count = 0

        for item in self._items:
            key = item.uri if item.uri else item.title.lower()
            if key in seen:
                self._release(item)
                removed_count += 1
            else:
                seen.add(key)
                unique_items.append(item)

        self._items = unique_items
        return removed_count

    # ------------------------------------------------------------- history
    def record_history(self, item: QueueItem) -> None:
        """Record a played track in bounded history."""
        if item.is_stream:
            return
        self._history.append(item)
        while len(self._history) > self.history_size:
            self._history.popleft()

    def pop_history(self) -> QueueItem | None:
        """Pop the most recent track from history."""
        return self._history.pop() if self._history else None

    def get_history(self, limit: int = 20) -> list[QueueItem]:
        """Return the most recent tracks in history (newest first)."""
        items = list(self._history)
        items.reverse()
        return items[:limit]

    def history_len(self) -> int:
        return len(self._history)

    @property
    def has_history(self) -> bool:
        return len(self._history) > 0

    # ----------------------------------------------------------- management
    def clear(self) -> int:
        count = len(self._items)
        self._items.clear()
        self._counts.clear()
        return count

    def shuffle(self, rng: random.Random | None = None) -> None:
        items = list(self._items)
        (rng or random).shuffle(items)
        self._items = deque(items)

    def page(self, page: int, per_page: int = 10) -> tuple[list[tuple[int, QueueItem]], int, int]:
        """Return (entries with 1-based positions, clamped page, total pages)."""
        total = len(self._items)
        pages = max(1, ceil(total / per_page))
        page = min(max(page, 1), pages)
        start = (page - 1) * per_page
        end = min(start + per_page, total)
        if start < 5000:
            chunk = list(islice(self._items, start, end))
        else:
            chunk = [self._items[i] for i in range(start, end)]
        return [(start + offset + 1, item) for offset, item in enumerate(chunk)], page, pages


def extract_user_avatar_url(user: Any) -> str | None:
    """Extract a static 128px PNG avatar URL from a discord.User or Member."""
    if user is None:
        return None
    avatar = getattr(user, "display_avatar", None)
    if avatar is None:
        return None
    try:
        return str(avatar.with_size(128).with_format("png").url)
    except Exception:
        return str(getattr(avatar, "url", "")) or None

