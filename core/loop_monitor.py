"""Supervised event-loop lag monitor and adaptive load shedding controller.

Measures event loop sleep drift using a rolling window of short sleeps.
Computes rolling p50, p95, p99, and max lag.
Triggers adaptive load shedding when p95 exceeds threshold for several seconds.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Callable

log = logging.getLogger(__name__)

DEFAULT_SAMPLE_INTERVAL = 0.1  # 100 ms
DEFAULT_WINDOW_SIZE = 100      # 10 seconds of 100ms samples
DEFAULT_LOAD_SHED_THRESHOLD_MS = 150.0
DEFAULT_WARN_THRESHOLD_MS = 250.0
CONSECUTIVE_SECS_TO_TRANSITION = 3.0


class LoopLagMonitor:
    def __init__(
        self,
        sample_interval: float = DEFAULT_SAMPLE_INTERVAL,
        window_size: int = DEFAULT_WINDOW_SIZE,
        load_shed_threshold_ms: float = DEFAULT_LOAD_SHED_THRESHOLD_MS,
        warn_threshold_ms: float = DEFAULT_WARN_THRESHOLD_MS,
        on_load_shedding_changed: Callable[[bool], None] | None = None,
    ) -> None:
        self.sample_interval = sample_interval
        self.window_size = window_size
        self.load_shed_threshold_ms = load_shed_threshold_ms
        self.warn_threshold_ms = warn_threshold_ms
        self.on_load_shedding_changed = on_load_shedding_changed

        self._samples: deque[float] = deque(maxlen=window_size)
        self._load_shedding: bool = False
        self._high_lag_start: float | None = None
        self._healthy_start: float | None = None
        self._stopped = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._stopped = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="loop-lag-monitor")

    def stop(self) -> None:
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    @property
    def is_load_shedding(self) -> bool:
        return self._load_shedding

    def stats(self) -> dict[str, float]:
        """Return dict with p50, p95, p99, and max in milliseconds."""
        if not self._samples:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
        sorted_samples = sorted(self._samples)
        n = len(sorted_samples)

        def pct(p: float) -> float:
            idx = max(0, min(n - 1, math.ceil(p * n) - 1))
            return sorted_samples[idx] * 1000.0

        return {
            "p50": pct(0.50),
            "p95": pct(0.95),
            "p99": pct(0.99),
            "max": sorted_samples[-1] * 1000.0,
        }

    @property
    def p50(self) -> float:
        return self.stats()["p50"]

    @property
    def p95(self) -> float:
        return self.stats()["p95"]

    @property
    def p99(self) -> float:
        return self.stats()["p99"]

    @property
    def max_lag(self) -> float:
        return self.stats()["max"]

    async def run(self) -> None:
        while not self._stopped:
            try:
                t0 = time.monotonic()
                await asyncio.sleep(self.sample_interval)
                t1 = time.monotonic()
                drift = max(0.0, (t1 - t0) - self.sample_interval)
                self._samples.append(drift)

                drift_ms = drift * 1000.0
                if drift_ms > self.warn_threshold_ms:
                    log.warning(
                        "Event loop lag spike detected: %.1f ms (warning threshold %.1f ms)",
                        drift_ms,
                        self.warn_threshold_ms,
                    )

                # Check p95 for adaptive load shedding
                st = self.stats()
                p95 = st["p95"]
                now = time.monotonic()

                if p95 >= self.load_shed_threshold_ms:
                    self._healthy_start = None
                    if self._high_lag_start is None:
                        self._high_lag_start = now
                    elif now - self._high_lag_start >= CONSECUTIVE_SECS_TO_TRANSITION and not self._load_shedding:
                        self._load_shedding = True
                        log.warning(
                            "Adaptive load shedding ACTIVATED: loop lag p95 is %.1f ms (threshold: %.1f ms)",
                            p95,
                            self.load_shed_threshold_ms,
                        )
                        if self.on_load_shedding_changed:
                            self.on_load_shedding_changed(True)
                else:
                    self._high_lag_start = None
                    if self._healthy_start is None:
                        self._healthy_start = now
                    elif now - self._healthy_start >= CONSECUTIVE_SECS_TO_TRANSITION and self._load_shedding:
                        self._load_shedding = False
                        log.info(
                            "Adaptive load shedding REVERTED: loop lag p95 recovered to %.1f ms (threshold: %.1f ms)",
                            p95,
                            self.load_shed_threshold_ms,
                        )
                        if self.on_load_shedding_changed:
                            self.on_load_shedding_changed(False)

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("LoopLagMonitor loop error; continuing")
                await asyncio.sleep(1.0)
