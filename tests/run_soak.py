"""Soak test: hundreds of create, play, kick and destroy cycles against fake players.

Run from the project root:

    python -m tests.run_soak --cycles 500 --guilds 25

It confirms that registry size, asyncio task count, Lavalink and voice state,
and memory all return to baseline. Exit code 0 means pass, 1 means fail.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import logging
import sys
import tracemalloc
from dataclasses import dataclass, field

from cogs.admin import HelpView
from core.contracts import PlayerServices
from core.queue import LoopMode, QueueItem
from core.registry import PlayerRegistry
from core.stats import rss_mb
from tests.fakes import FakeBackend, FakeLoader, make_config, make_track
from utils.cache import TTLCache
from utils.components_v2 import ACTIVE_VIEWS, PaginatedPage, PaginatedView
from utils.errors import BotUserError
from utils.ratelimit import get_limiter
from utils.timing import clear_timing_buffers, reset_http_429_count

log = logging.getLogger(__name__)

TRACE_LIMIT_KB = 1024.0


@dataclass
class SoakReport:
    cycles: int
    registry_size: int = 0
    task_delta: int = 0
    traced_kb: float = 0.0
    rss_delta_mb: float = 0.0
    leftover_audio: int = 0
    leftover_voice: int = 0
    leftover_cards: int = 0
    leftover_views: int = 0
    leftover_limiter_keys: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


async def run_unlimited_queue_scenario(registry: PlayerRegistry) -> None:
    """Exercise an unlimited queue with 100,000 tracks, large chunked imports, paging, shuffle, and skip."""
    large_guild_id = 88888
    large_player = await registry.get_or_create(large_guild_id, 188888, 288888)
    chunk_size = 5000
    total_tracks = 100_000
    for chunk_start in range(0, total_tracks, chunk_size):
        chunk = [
            QueueItem.from_track(make_track(chunk_start + k), requester_id=1)
            for k in range(chunk_size)
        ]
        await large_player.enqueue(chunk)
        await asyncio.sleep(0)

    assert len(large_player.queue) == total_tracks - 1
    _ = large_player.queue.page(500, per_page=10)
    large_player.queue.shuffle()
    if large_player.current is not None:
        await large_player.skip()
    await registry.destroy(large_guild_id, "soak_large_queue_end")


async def run_soak(cycles: int = 500, guilds: int = 25, rss_limit_mb: float | None = None) -> SoakReport:
    cfg = make_config()
    backend = FakeBackend()
    loader = FakeLoader()
    registry = PlayerRegistry(PlayerServices(cfg, backend, loader))
    autocomplete_cache: TTLCache[str, list[str]] = TTLCache(max_size=50, ttl=60.0)

    async def one_cycle(i: int) -> None:
        guild_id = i % guilds + 1
        player = await registry.get_or_create(guild_id, 1000 + guild_id, 2000 + guild_id)
        items = [
            QueueItem.from_track(
                make_track(i * 10 + k),
                requester_id=(i % 7) + 1,
                query="song" if (i % 2 == 0 and k == 0) else None,
            )
            for k in range(4)
        ]
        try:
            await player.enqueue(items)
        except BotUserError as exc:
            log.debug("soak enqueue expected error: %s", exc)

        # Let spawned card creation task execute
        await asyncio.sleep(0)

        player.start_timer("alone", 300, "alone")
        player.set_sleep(30 if i % 2 == 0 else 0)
        player.set_autoplay(i % 3 == 0)
        if i % 4 == 0:
            player.applied_filters.add("nightcore")
            player.current_eq = "bassboost"

        # Exercise card updates, loop modes, and channel moves
        if i % 3 == 0:
            player.set_loop(LoopMode.TRACK if i % 2 == 0 else LoopMode.OFF)
        if i % 4 == 0 and player.current is not None:
            await player.pause()
            await asyncio.sleep(0)
            await player.resume()
        if i % 7 == 0:
            await player.move_card(3000 + guild_id)
            await asyncio.sleep(0)

        # Exercise Rate Limiter
        limiter = get_limiter()
        limiter.acquire_command(user_id=100 + (i % 7), guild_id=guild_id, command_name="play")

        # Exercise Components V2 views
        if i % 5 == 0:
            p_view = PaginatedView(
                items_provider=lambda p: PaginatedPage(
                    title="Soak Queue",
                    items=[f"Item {k}" for k in range((p - 1) * 10, min(25, p * 10))],
                    current_page=p,
                    total_pages=3,
                ),
                author_id=100 + (i % 7),
                guild_id=guild_id,
                kind="queue",
            )
            await ACTIVE_VIEWS.register(p_view)
            await p_view.render(page_num=1)
            await p_view.render(page_num=2)
            p_view.disable_all_buttons()
            await p_view.on_timeout()
            p_view.stop()

        if i % 9 == 0:
            h_view = HelpView(
                categories=["Overview", "Playback"],
                descriptions={"Playback": "Audio"},
                commands_by_cat={"Playback": ["/play - play", "/pause - pause"]},
                author_id=100 + (i % 7),
            )
            await h_view.render()
            h_view.disable_all_buttons()
            await h_view.on_timeout()
            h_view.stop()

        # Exercise Autocomplete TTLCache
        query = f"soak_query_{i % 20}"
        if query not in autocomplete_cache:
            candidates = await loader.search_candidates(query, limit=5)
            autocomplete_cache[query] = [c[0] for c in candidates]
        else:
            _ = autocomplete_cache.get(query)

        if player.current is not None:
            try:
                await player.vote_skip(user_id=100 + (i % 5), humans_count=4)
            except Exception as exc:
                log.debug("soak vote skip expected error: %s", exc)

        mode = i % 5
        if mode == 0 and player.current is not None:
            await player.on_track_end(player.current.track.track, "finished")
            await asyncio.sleep(0)
        elif mode == 1:
            backend.voice.pop(guild_id, None)  # simulate being kicked
            await registry.reconcile("soak")
        elif mode == 2:
            await registry.destroy(guild_id, "soak")
        elif mode == 3 and player.current is not None:
            await player.on_track_exception(player.current.track.track, "simulated failure")
            await registry.destroy(guild_id, "soak")
        elif player.current is not None:
            await player.skip()
            await registry.destroy(guild_id, "soak")

        await asyncio.sleep(0)

    for i in range(30):  # warm up caches and one-time allocations
        await one_cycle(i)
    await registry.destroy_all("warmup")
    await asyncio.sleep(0.05)
    gc.collect()

    baseline_tasks = len(asyncio.all_tasks())
    baseline_rss = rss_mb()
    started_tracing = not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start()
    baseline_traced = tracemalloc.get_traced_memory()[0]

    for i in range(cycles):
        await one_cycle(i)

    # Unlimited queue scenario: 100,000 tracks and large imports
    await run_unlimited_queue_scenario(registry)

    await registry.destroy_all("end of soak")
    await ACTIVE_VIEWS.close_all()
    limiter = get_limiter()
    limiter.prune_idle(max_idle_seconds=0.0)
    clear_timing_buffers()
    reset_http_429_count()
    await asyncio.sleep(0.1)
    gc.collect()

    report = SoakReport(cycles=cycles)
    report.registry_size = len(registry)
    report.task_delta = len(asyncio.all_tasks()) - baseline_tasks
    report.traced_kb = (tracemalloc.get_traced_memory()[0] - baseline_traced) / 1024.0
    report.rss_delta_mb = rss_mb() - baseline_rss
    report.leftover_audio = len(backend.audios)
    report.leftover_voice = len(backend.voice)
    report.leftover_cards = len(backend.active_cards)
    report.leftover_views = len(ACTIVE_VIEWS._views)
    report.leftover_limiter_keys = len(limiter._windows)
    if started_tracing:
        tracemalloc.stop()

    if report.registry_size != 0:
        report.problems.append(f"registry still holds {report.registry_size} players")
    if report.task_delta != 0:
        report.problems.append(f"task count changed by {report.task_delta}")
    if report.leftover_audio or report.leftover_voice:
        report.problems.append(
            f"leftover fake state: audio={report.leftover_audio} voice={report.leftover_voice}"
        )
    if report.leftover_cards != 0:
        report.problems.append(f"leftover active cards: {report.leftover_cards}")
    if report.leftover_views != 0:
        report.problems.append(f"leftover active views: {report.leftover_views}")
    if report.leftover_limiter_keys != 0:
        report.problems.append(f"leftover limiter keys: {report.leftover_limiter_keys}")
    if report.traced_kb > TRACE_LIMIT_KB:
        report.problems.append(f"traced memory grew by {report.traced_kb:.0f} KB")
    if rss_limit_mb is not None and report.rss_delta_mb > rss_limit_mb:
        report.problems.append(f"RSS grew by {report.rss_delta_mb:.1f} MB")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Soak test for the guild player registry")
    parser.add_argument("--cycles", type=int, default=500)
    parser.add_argument("--guilds", type=int, default=25)
    parser.add_argument("--rss-limit-mb", type=float, default=25.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    report = asyncio.run(run_soak(args.cycles, args.guilds, args.rss_limit_mb))
    print(f"cycles          : {report.cycles}")
    print(f"registry size   : {report.registry_size}")
    print(f"task delta      : {report.task_delta}")
    print(f"traced memory   : {report.traced_kb:.1f} KB growth")
    print(f"rss             : {report.rss_delta_mb:.1f} MB growth")
    print(f"leftover state  : audio={report.leftover_audio} voice={report.leftover_voice} cards={report.leftover_cards}")
    if report.ok:
        print("RESULT: PASS")
        return 0
    for problem in report.problems:
        print(f"PROBLEM: {problem}")
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
