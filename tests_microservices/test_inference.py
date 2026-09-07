import asyncio
import threading
import pytest
from fastapi import HTTPException
from services.inference.api import InferenceScheduler


@pytest.mark.asyncio
async def test_online_priority_and_cancelled_queue_are_respected():
    scheduler = InferenceScheduler(None, None, 4)
    started = threading.Event()
    release = threading.Event()
    order = []

    def blocking():
        started.set()
        release.wait(3)
        order.append("running")

    first = asyncio.create_task(scheduler.submit(blocking, 1))
    await asyncio.to_thread(started.wait, 2)
    offline = asyncio.create_task(scheduler.submit(lambda: order.append("offline"), 1))
    online = asyncio.create_task(scheduler.submit(lambda: order.append("online"), 0))
    cancelled = asyncio.create_task(
        scheduler.submit(lambda: order.append("cancelled"), 0)
    )
    await asyncio.sleep(0.01)
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    release.set()
    await asyncio.gather(first, offline, online)
    await scheduler.close()
    assert order == ["running", "online", "offline"]


@pytest.mark.asyncio
async def test_inference_queue_is_bounded():
    scheduler = InferenceScheduler(None, None, 1)
    started = threading.Event()
    release = threading.Event()
    first = asyncio.create_task(
        scheduler.submit(lambda: (started.set(), release.wait(3)))
    )
    await asyncio.to_thread(started.wait, 2)
    second = asyncio.create_task(scheduler.submit(lambda: 1))
    await asyncio.sleep(0.01)
    with pytest.raises(HTTPException) as exc:
        await scheduler.submit(lambda: 2)
    assert exc.value.status_code == 429
    release.set()
    await asyncio.gather(first, second)
    await scheduler.close()
