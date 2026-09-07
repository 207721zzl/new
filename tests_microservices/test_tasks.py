from datetime import timedelta
import pytest
from services.chat.db import session_factory
from services.chat.models import DurableTask
from services.chat.runs import AgentRunStore
from packages.platform.tasks import (
    TaskRunner,
    enqueue,
    publish_pending,
    now,
    current_lease,
    Lease,
    LeaseLost,
    guard,
)


@pytest.mark.asyncio
async def test_task_and_dispatch_intent_commit_together(databases):
    run = await AgentRunStore().create("run-1", "employee-1", "问题", 5)
    async with session_factory() as session:
        task = await session.get(DurableTask, run.run_id)
        assert task.status == "pending" and task.resource_id == run.run_id


@pytest.mark.asyncio
async def test_failed_broker_publish_remains_recoverable(databases):
    async with session_factory() as session:
        enqueue(session, DurableTask, "answer", "run", "task")
        await session.commit()

    async def unavailable(_):
        raise ConnectionError("broker down")

    with pytest.raises(ConnectionError):
        await publish_pending(DurableTask, session_factory, unavailable)
    async with session_factory() as session:
        assert (await session.get(DurableTask, "task")).published_at is None
    sent = []

    async def send(task_id):
        sent.append(task_id)

    await publish_pending(DurableTask, session_factory, send)
    assert sent == ["task"]


@pytest.mark.asyncio
async def test_duplicate_delivery_has_one_business_execution(databases):
    async with session_factory() as session:
        enqueue(session, DurableTask, "answer", "run", "task")
        await session.commit()
    calls = []

    async def handle(task):
        calls.append(task.task_id)

    async def fail(task):
        pytest.fail("should not fail")

    runner = TaskRunner(DurableTask, session_factory, handle, fail)
    await runner.execute("task")
    await runner.execute("task")
    assert calls == ["task"]


@pytest.mark.asyncio
async def test_new_worker_does_not_steal_live_lease_and_old_worker_is_fenced(databases):
    async with session_factory() as session:
        enqueue(session, DurableTask, "answer", "run", "task")
        await session.commit()
    runner = TaskRunner(DurableTask, session_factory, None, None)
    first = await runner.claim("task")
    assert await runner.claim("task") is None
    async with session_factory() as session:
        task = await session.get(DurableTask, "task")
        task.lease_until = now() - timedelta(seconds=1)
        await session.commit()
    second = await runner.claim("task")
    assert second.generation == first.generation + 1
    token = current_lease.set(Lease(DurableTask, "task", first.owner, first.generation))
    try:
        async with session_factory() as session:
            with pytest.raises(LeaseLost):
                await guard(session)
    finally:
        current_lease.reset(token)


@pytest.mark.asyncio
async def test_retry_exhaustion_is_terminal_and_does_not_spin(databases):
    async with session_factory() as session:
        enqueue(session, DurableTask, "answer", "run", "task")
        await session.commit()
    failures = []

    async def handle(task):
        raise RuntimeError("transient")

    async def fail(task):
        failures.append(task.task_id)

    runner = TaskRunner(DurableTask, session_factory, handle, fail)
    for _ in range(4):
        await runner.execute("task")
        async with session_factory() as session:
            row = await session.get(DurableTask, "task")
            row.available_at = now() - timedelta(seconds=1)
            await session.commit()
    async with session_factory() as session:
        row = await session.get(DurableTask, "task")
        assert row.status == "failed" and row.attempts == 3
    assert failures == ["task"]
