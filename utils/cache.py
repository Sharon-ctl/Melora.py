"""Bounded in-memory structures: a TTL cache and a cooldown tracker."""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """Least-recently-used cache with per-entry expiry and a hard size limit."""

    def __init__(self, max_size: int, ttl: float, clock: Callable[[], float] = time.monotonic) -> None:
        if max_size < 1 or ttl <= 0:
            raise ValueError("max_size must be >= 1 and ttl must be > 0")
        self._max_size = max_size
        self._ttl = ttl
        self._clock = clock
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._last_prune = clock()

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: K) -> bool:
        return self.get(key) is not None

    def __getitem__(self, key: K) -> V:
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __setitem__(self, key: K, value: V) -> None:
        self.set(key, value)

    def get(self, key: K, default: V | None = None) -> V | None:
        entry = self._data.get(key)
        if entry is None:
            return default
        expires, value = entry
        if expires <= self._clock():
            del self._data[key]
            return default
        self._data.move_to_end(key)
        return value

    def set(self, key: K, value: V) -> None:
        now = self._clock()
        self._data[key] = (now + self._ttl, value)
        self._data.move_to_end(key)
        if now - self._last_prune >= self._ttl / 2:
            self.prune()
        while len(self._data) > self._max_size:
            self._data.popitem(last=False)

    def prune(self) -> None:
        now = self._clock()
        self._last_prune = now
        for key in [k for k, (expires, _) in self._data.items() if expires <= now]:
            del self._data[key]

    def clear(self) -> None:
        self._data.clear()

    def invalidate(self, key: K) -> None:
        """Remove a key from cache if present."""
        self._data.pop(key, None)


class CooldownTracker:
    """Per-key cooldowns with expiry pruning and a maximum number of records."""

    def __init__(self, seconds: float, max_size: int = 2048, clock: Callable[[], float] = time.monotonic) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._seconds = seconds
        self._max_size = max_size
        self._clock = clock
        self._expiry: OrderedDict[Hashable, float] = OrderedDict()

    def __len__(self) -> int:
        return len(self._expiry)

    def hit(self, key: Hashable) -> float:
        """Record a use. Returns 0 if allowed, otherwise the seconds left to wait."""
        if self._seconds <= 0:
            return 0.0
        now = self._clock()
        while self._expiry:
            oldest_key, oldest_expiry = next(iter(self._expiry.items()))
            if oldest_expiry > now:
                break
            del self._expiry[oldest_key]
        current = self._expiry.get(key)
        if current is not None and current > now:
            return current - now
        self._expiry[key] = now + self._seconds
        self._expiry.move_to_end(key)
        while len(self._expiry) > self._max_size:
            self._expiry.popitem(last=False)
        return 0.0

    trigger = hit

