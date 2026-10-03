import asyncio

from utils.supervisor import TaskSet, TaskSupervisor


def test_supervisor_restarts_failed_task_and_starts_it_once():
    async def scenario():
        supervisor = TaskSupervisor()
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom")
            await asyncio.sleep(30)

        first = supervisor.start("worker", flaky, min_backoff=0.01, max_backoff=0.02)
        second = supervisor.start("worker", flaky, min_backoff=0.01, max_backoff=0.02)
        assert first is second
        await asyncio.sleep(0.3)
        assert calls["n"] == 3
        assert supervisor.live_count() == 1
        await supervisor.stop_all()
        assert supervisor.live_count() == 0

    asyncio.run(scenario())


def test_one_shot_task_can_start_again_after_it_finishes():
    async def scenario():
        supervisor = TaskSupervisor()
        runs = []

        async def once():
            runs.append(1)

        supervisor.start("sweep", once, restart=False)
        await asyncio.sleep(0.05)
        supervisor.start("sweep", once, restart=False)
        await asyncio.sleep(0.05)
        assert len(runs) == 2

    asyncio.run(scenario())


def test_taskset_is_bounded_and_cancels_everything():
    async def scenario():
        tasks = TaskSet("test", max_tasks=2)
        a = tasks.spawn(asyncio.sleep(30), name="a")
        b = tasks.spawn(asyncio.sleep(30), name="b")
        c = tasks.spawn(asyncio.sleep(30), name="c")
        assert a is not None and b is not None and c is None
        tasks.cancel_all()
        await asyncio.sleep(0.01)
        assert len(tasks) == 0
        assert tasks.spawn(asyncio.sleep(1), name="late") is None

    asyncio.run(scenario())


def test_taskset_cancel_all_does_not_cancel_the_caller():
    async def scenario():
        tasks = TaskSet("test")
        finished = []

        async def worker():
            tasks.cancel_all()
            await asyncio.sleep(0.01)
            finished.append(True)

        task = tasks.spawn(worker(), name="self-cancel")
        assert task is not None
        await asyncio.sleep(0.1)
        assert finished == [True]

    asyncio.run(scenario())
