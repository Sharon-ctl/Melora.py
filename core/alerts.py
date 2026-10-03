"""Optional, rate-limited webhook alerts to the owner for critical errors."""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from config import Config

log = logging.getLogger(__name__)

QUEUE_LIMIT = 20
MAX_TEXT = 500


class Alerter:
    """submit() is safe from synchronous code; run() is a supervised worker task."""

    def __init__(self, cfg: Config) -> None:
        self._url = cfg.alert_webhook_url
        self._owner_id = cfg.owner_id
        self._min_interval = cfg.alert_min_interval
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._session: aiohttp.ClientSession | None = None
        self._last_sent = float("-inf")
        self._suppressed = 0

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def submit(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(text[:MAX_TEXT])
        except asyncio.QueueFull:
            self._suppressed += 1

    async def run(self) -> None:
        while True:
            text = await self._queue.get()
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_sent)
            if wait > 0:
                await asyncio.sleep(wait)
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._suppressed += 1
                except asyncio.QueueEmpty:
                    break
            suffix = f" ({self._suppressed} more alerts suppressed)" if self._suppressed else ""
            self._suppressed = 0
            await self._send(text + suffix)
            self._last_sent = time.monotonic()

    async def _send(self, text: str) -> None:
        payload = {
            "content": f"<@{self._owner_id}> {text}"[:1900],
            "allowed_mentions": {"users": [str(self._owner_id)]},
        }
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
            async with self._session.post(self._url, json=payload) as response:
                if response.status >= 400:
                    log.warning("Alert webhook returned status %s", response.status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Only the exception type is logged: the message could contain the webhook URL.
            log.warning("Alert webhook delivery failed: %s", type(exc).__name__)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
