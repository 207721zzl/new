from datetime import timedelta
from fastapi import APIRouter
from sqlalchemy import select, func
from services.knowledge.db import session_factory
from services.knowledge.models import DocumentHead, DocumentSnapshot, IndexingJob
from app.config import get_settings
from packages.contracts.metrics import workload
from packages.platform.tasks import now

router=APIRouter(prefix="/internal/v1")


@router.post("/stats")
async def stats():
    config=get_settings()
    async with session_factory() as session:
        snapshots=list(await session.scalars(select(DocumentSnapshot).join(DocumentHead,
            DocumentHead.active_snapshot==DocumentSnapshot.snapshot_id).where(DocumentHead.deleted_at.is_(None))))
        jobs=list(await session.scalars(select(IndexingJob).where(IndexingJob.created_at >= now()-timedelta(hours=config.pilot_metrics_window_hours))))
        size=await session.scalar(select(func.sum(IndexingJob.file_size_bytes)).where(IndexingJob.status=="completed"))
    return {"knowledge":{"documents":len(snapshots),"parent_chunks":sum(len(s.payload["parents"]) for s in snapshots),
            "child_chunks":sum(len(s.payload["children"]) for s in snapshots),"indexed_source_bytes":int(size or 0)},
            "indexing":workload([(j.status,j.started_at,j.completed_at,j.updated_at) for j in jobs],now()-timedelta(minutes=config.pilot_stale_task_minutes))}
