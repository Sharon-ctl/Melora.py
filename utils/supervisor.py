"""Task supervision.

``TaskSupervisor`` keeps named long-running background tasks alive: it logs
failures, restarts with exponential backoff, and refuses to start a second
copy of a task that is already running.

``TaskSet`` owns the short-lived tasks of one object (for example one guild
player). It is bounded, logs failures, and cancels everything on close.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

log = logging.getLogger(__name__)

CriticalCallback = Callable[[str, int, BaseException], Awaitable[None]]


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError as exc:
        log.debug("_current_task query failed: %s", exc)
        return None


class TaskSet:
    """A bounded group of tasks that are always observed and always cancellable."""

    def __init__(self, label: str, max_tasks: int = 64) -> None:
        self._label = label
        self._max_tasks = max_tasks
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def __len__(self) -> int:
        return len(self._tasks)

    @property
    def closed(self) -> bool:
        return self._closed

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any] | None:
        """Start a task. Returns None (and closes the coroutine) if refused."""
        if self._closed or len(self._tasks) >= self._max_tasks:
            coro.close()
            if not self._closed:
                log.warning("Task limit reached for %s; dropped %s", self._label, name)
            return None
        task = asyncio.get_running_loop().create_task(coro, name=f"{self._label}:{name}")
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("Task %s failed", task.get_name(), exc_info=(type(exc), exc, exc.__traceback__))

    def cancel_all(self) -> None:
        """Close the set and cancel every task except the one calling this."""
        self._closed = True
        current = _current_task()
        for task in list(self._tasks):
            if task is not current and not task.done():
                task.cancel()

    async def wait_closed(self, timeout: float = 5.0) -> None:
        current = _current_task()
        pending = [t for t in self._tasks if t is not current]
        if pending:
            await asyncio.wait(pending, timeout=timeout)


class TaskSupervisor:
    """Runs named background tasks and restarts them when they fail."""

    def __init__(self, on_critical: CriticalCallback | None = None) -> None:
        self._on_critical = on_critical
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._short_tasks = TaskSet("supervised-short", max_tasks=256)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any] | None:
        """Start a short-lived task bounded by TaskSet."""
        return self._short_tasks.spawn(coro, name=name)

    def start(
        self,
        name: str,
        factory: Callable[[], Awaitable[Any]],
        *,
        restart: bool = True,
        min_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> asyncio.Task[Any]:
        """Start a task by name. If it is already running, return the existing task."""
        existing = self._tasks.get(name)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.get_running_loop().create_task(
            self._run(name, factory, restart, min_backoff, max_backoff),
            name=f"supervised:{name}",
        )
        self._tasks[name] = task
        task.add_done_callback(lambda finished, key=name: self._finished(key, finished))
        return task

    def _finished(self, name: str, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(name) is task:
            del self._tasks[name]

    async def _run(
        self,
        name: str,
        factory: Callable[[], Awaitable[Any]],
        restart: bool,
        min_backoff: float,
        max_backoff: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        backoff = min_backoff
        failures = 0
        while True:
            started = loop.time()
            try:
                await factory()
                if not restart:
                    return
                log.warning("Supervised task %s returned; restarting in %.1fs", name, backoff)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                log.error(
                    "Supervised task %s crashed (failure %d)",
                    name,
                    failures,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                if not restart:
                    return
                await self._escalate(name, failures, exc)
            if loop.time() - started > 60.0:
                backoff = min_backoff
                failures = 0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def _escalate(self, name: str, failures: int, exc: BaseException) -> None:
        if self._on_critical is None:
            return
        if failures not in (3, 10) and failures % 50 != 0:
            return
        try:
            await asyncio.wait_for(self._on_critical(name, failures, exc), timeout=10.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Critical callback failed for task %s", name)

    def is_running(self, name: str) -> bool:
        task = self._tasks.get(name)
        return task is not None and not task.done()

    def live_count(self) -> int:
        return sum(1 for task in self._tasks.values() if not task.done())

    def names(self) -> list[str]:
        return sorted(name for name, task in self._tasks.items() if not task.done())

    async def stop_all(self, timeout: float = 10.0) -> None:
        self._short_tasks.cancel_all()
        await self._short_tasks.wait_closed(timeout=timeout)
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        self._tasks.clear()
