import asyncio

from core.contracts import PlayerServices
from core.queue import LoopMode, QueueItem
from core.registry import PlayerRegistry
from tests.fakes import FakeBackend, FakeLoader, make_config, make_track
from tests.run_soak import run_soak


def build(**overrides):
    cfg = make_config(**overrides)
    backend = FakeBackend()
    loader = FakeLoader()
    registry = PlayerRegistry(PlayerServices(cfg, backend, loader))
    return registry, backend, loader


def items(count: int, query: str | None = None, user: int = 1):
    return [QueueItem.from_track(make_track(n), user, query=query) for n in range(count)]


def run(coro):
    return asyncio.run(coro)


def test_enqueue_starts_first_track_and_tracks_position():
    async def scenario():
        registry, backend, _ = build()
        player = await registry.get_or_create(1, 10, 20)
        first = await player.enqueue(items(3))
        assert first.started and first.position == 0 and player.current is not None
        assert len(player.queue) == 2
        second = await player.enqueue(items(1))
        assert not second.started and second.position == 3
        await registry.destroy_all("test")

    run(scenario())


def test_track_end_advances_and_empty_queue_sets_idle_timer():
    async def scenario():
        registry, backend, _ = build()
        player = await registry.get_or_create(1, 10, 20)
        await player.enqueue(items(2))
        await player.on_track_end(player.current.track.track, "finished")
        assert player.current is not None and len(player.queue) == 0
        await player.on_track_end(player.current.track.track, "finished")
        assert player.current is None and player.has_timer("idle")
        await registry.destroy_all("test")

    run(scenario())


def test_replaced_and_stopped_end_events_are_ignored():
    async def scenario():
        registry, backend, _ = build()
        player = await registry.get_or_create(1, 10, 20)
        await player.enqueue(items(2))
        current = player.current
        await player.on_track_end(current.track.track, "replaced")
        await player.on_track_end(current.track.track, "stopped")
        assert player.current is current
        await registry.destroy_all("test")

    run(scenario())


def test_exception_then_load_failed_end_skips_only_once():
    async def scenario():
        registry, backend, _ = build()
        player = await registry.get_or_create(1, 10, 20)
        await player.enqueue(items(3))
        failing = player.current
        await player.on_track_exception(failing.track.track, "boom")
        after_first = player.current
        assert after_first is not failing
        await player.on_track_end(failing.track.track, "loadFailed")
        assert player.current is after_first
        assert len(player.queue) == 1
        await registry.destroy_all("test")

    run(scenario())


def test_loop_track_replays_current():
    async def scenario():
        registry, backend, _ = build()
        player = await registry.get_or_create(1, 10, 20)
        await player.enqueue(items(2))
        player.set_loop(LoopMode.TRACK)
        current = player.current
        await player.on_track_end(current.track.track, "finished")
        assert player.current is current
        await registry.destroy_all("test")

    run(scenario())


def test_circuit_breaker_stops_after_consecutive_failures():
    async def scenario():
        registry, backend, _ = build(FAILURE_BREAKER=3)
        player = await registry.get_or_create(1, 10, 20)
        backend.audio(1).always_fail = True
        await player.enqueue(items(6))
        assert backend.audio(1).plays == 3
        assert player.current is None and len(player.queue) == 0
        await registry.destroy_all("test")

    run(scenario())


def test_search_item_falls_back_once_when_start_fails():
    async def scenario():
        registry, backend, loader = build()
        player = await registry.get_or_create(1, 10, 20)
        backend.audio(1).fail_next = 1
        await player.enqueue(items(1, query="some song"))
        assert loader.fallback_calls == 1
        assert player.current is not None and player.current.fallback_used
        assert player.current.title.startswith("Fallback for")
        await registry.destroy_all("test")

    run(scenario())


def test_direct_link_never_uses_fallback():
    async def scenario():
        registry, backend, loader = build()
        player = await registry.get_or_create(1, 10, 20)
        backend.audio(1).fail_next = 1
        await player.enqueue(items(1, query=None))
        assert loader.fallback_calls == 0
        await registry.destroy_all("test")

    run(scenario())


def test_ghost_player_is_rebuilt_before_play():
    async def scenario():
        registry, backend, _ = build()
        old = await registry.get_or_create(1, 10, 20)
        backend.voice.pop(1)  # voice connection vanished while the player was tracked
        new = await registry.get_or_create(1, 10, 20)
        assert new is not old and old.destroyed
        assert backend.voice_connected(1, 10)
        await registry.destroy_all("test")

    run(scenario())


def test_reconcile_destroys_players_without_voice_and_leftovers():
    async def scenario():
        registry, backend, _ = build()
        await registry.get_or_create(1, 10, 20)
        await registry.get_or_create(2, 11, 21)
        backend.voice.pop(1)
        backend.audios[99] = backend.audios[2]  # leftover from a previous process
        backend.voice[99] = 5
        fixed = await registry.reconcile("test")
        assert fixed == 2
        assert registry.get(1) is None and registry.get(2) is not None
        assert 99 not in backend.audios and 99 not in backend.voice
        await registry.destroy_all("test")

    run(scenario())


def test_alone_timer_is_ignored_when_someone_is_present():
    async def scenario():
        registry, backend, _ = build()
        await registry.get_or_create(1, 10, 20)
        backend.humans[1] = 2
        await registry.expire(1, "alone")
        assert registry.get(1) is not None
        backend.humans[1] = 0
        await registry.expire(1, "alone")
        assert registry.get(1) is None

    run(scenario())


def test_all_nodes_down_destroys_players():
    async def scenario():
        registry, backend, _ = build()
        await registry.get_or_create(1, 10, 20)
        await registry.get_or_create(2, 11, 21)
        assert await registry.handle_all_nodes_down() == 0
        backend.nodes_up = False
        assert await registry.handle_all_nodes_down() == 2
        assert len(registry) == 0 and backend.notices >= 2

    run(scenario())


def test_destroy_is_idempotent_and_cancels_tasks():
    async def scenario():
        registry, backend, _ = build()
        player = await registry.get_or_create(1, 10, 20)
        player.tasks.spawn(asyncio.sleep(300), name="test-task")
        baseline = len(asyncio.all_tasks())
        assert await registry.destroy(1, "test") is True
        assert await registry.destroy(1, "test") is False
        await asyncio.sleep(0.01)
        assert len(asyncio.all_tasks()) < baseline
        assert player.destroyed and len(player.tasks) == 0

    run(scenario())


def test_short_soak_returns_to_baseline():
    report = run(run_soak(cycles=100, guilds=10))
    assert report.ok, report.problems
