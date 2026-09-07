import asyncio
from contextlib import suppress
from dataclasses import asdict
from itertools import count
from fastapi import HTTPException
from app.config import get_settings
from app.rag.embeddings import BgeM3Embedder
from app.rag.reranker import BgeReranker
from packages.contracts.rag import EmbedRequest, RerankRequest
from packages.platform.application import create_app
from packages.platform.config import settings


class InferenceScheduler:
    """One consumer owns the GPU. Cancelled queued requests never enter inference."""
    def __init__(self, embedder, reranker, max_pending):
        self.embedder, self.reranker = embedder, reranker
        self.queue = asyncio.PriorityQueue(max_pending)
        self.sequence = count()
        self.worker = asyncio.create_task(self.consume())

    async def consume(self):
        while True:
            _, _, function, future = await self.queue.get()
            try:
                if future.cancelled():
                    continue
                result = await asyncio.to_thread(function)
                if not future.done():
                    future.set_result(result)
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                self.queue.task_done()

    async def submit(self, function, priority=0):
        future = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait((priority, next(self.sequence), function, future))
        except asyncio.QueueFull:
            raise HTTPException(429, "推理队列已满，请稍后重试。")
        try:
            return await asyncio.wait_for(future, timeout=get_settings().inference_queue_timeout_seconds + 120)
        except TimeoutError:
            raise HTTPException(503, "模型推理超时。")

    async def close(self):
        await self.queue.join()
        self.worker.cancel()
        with suppress(asyncio.CancelledError):
            await self.worker


async def startup():
    config = get_settings()
    embedder = await asyncio.to_thread(BgeM3Embedder, config)
    reranker = await asyncio.to_thread(BgeReranker, config)
    await asyncio.to_thread(embedder.encode, ["模型预热"])
    await asyncio.to_thread(reranker.rerank, "模型预热", ["模型预热"], 1)
    app.state.scheduler = InferenceScheduler(embedder, reranker, settings().inference_max_pending)


async def shutdown():
    await app.state.scheduler.close()


app = create_app("inference", startup=startup, shutdown=shutdown)


def validate_texts(texts):
    if any(not item.strip() or len(item) > 16000 for item in texts) or sum(map(len, texts)) > 128000:
        raise HTTPException(422, "文本为空或超过推理批次上限。")


@app.post("/internal/v1/embed")
async def embed(payload: EmbedRequest):
    validate_texts(payload.texts)
    scheduler = app.state.scheduler
    vectors = await scheduler.submit(lambda: scheduler.embedder.encode(payload.texts), payload.priority)
    return {"vectors": vectors, "dimension": get_settings().embedding_dimension}


@app.post("/internal/v1/rerank")
async def rerank(payload: RerankRequest):
    validate_texts(payload.documents)
    scheduler = app.state.scheduler
    results = await scheduler.submit(lambda: scheduler.reranker.rerank(payload.query, payload.documents, payload.top_k))
    return {"results": [asdict(item) for item in results]}


@app.get("/api/v1/health/ready")
async def ready():
    if not hasattr(app.state, "scheduler"):
        raise HTTPException(503, "模型尚未就绪。")
    return {"status": "ready", "queue_depth": app.state.scheduler.queue.qsize()}
