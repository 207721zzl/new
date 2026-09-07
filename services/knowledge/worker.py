from sqlalchemy import select
from services.knowledge.db import database as database, session_factory
from services.knowledge.models import DurableTask, IndexingJob
from services.knowledge.indexing import index_job, rebuild_batch
from services.knowledge.deletion import cleanup
from packages.platform.tasks import TaskRunner, guard, now


async def handle(task):
    if task.kind == "audit":
        from services.knowledge.models import KnowledgeAudit
        from packages.platform.client import ServiceClient
        async with session_factory() as session:
            audit = await session.get(KnowledgeAudit, task.resource_id)
            payload = {key: getattr(audit, key) for key in ("audit_id", "actor_user_id", "action", "document_id", "details")}
        await ServiceClient("identity").post("/internal/v1/audit", payload)
        return
    if task.kind == "delete":
        return await cleanup(task.resource_id)
    async with session_factory() as session:
        jobs = list(await session.scalars(select(IndexingJob).where(IndexingJob.batch_id == task.resource_id)
            .order_by(IndexingJob.recreate.desc(), IndexingJob.created_at, IndexingJob.job_id)))
    for job in jobs:
        if any(j.recreate for j in jobs) and any(j.status != "completed" for j in jobs):
            return await rebuild_batch(jobs)
        if job.status != "completed":
            await index_job(job)


async def failed(task):
    async with session_factory() as session:
        await guard(session)
        jobs = list(await session.scalars(select(IndexingJob).where(IndexingJob.batch_id == task.resource_id,
            IndexingJob.status != "completed")))
        for job in jobs:
            job.status, job.completed_at = "failed", now()
            job.error_message = "入库多次执行失败，请检查文档或依赖服务后重新上传。"
        await session.commit()


def runner():
    return TaskRunner(DurableTask, session_factory, handle, failed)
