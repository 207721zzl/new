"""Remove unreachable versions and upload objects after a grace period. Preview by default."""

import argparse
import asyncio
from datetime import timedelta, UTC
from sqlalchemy import select
from app.config import get_settings
from app.rag.milvus_store import MilvusKnowledgeStore
from packages.platform.tasks import now
from services.knowledge.db import session_factory, database
from services.knowledge.models import DocumentSnapshot, DocumentHead, IndexingJob
from services.knowledge.objects import ObjectStore
from packages.platform.config import settings


async def reconcile(execute=False):
    cutoff = now() - timedelta(days=1)
    async with session_factory() as session:
        active = set(
            await session.scalars(
                select(DocumentHead.active_snapshot).where(
                    DocumentHead.active_snapshot.is_not(None)
                )
            )
        )
        jobs = list(await session.scalars(select(IndexingJob)))
        running = {j.job_id for j in jobs if j.status in {"pending", "running"}}
        snapshots = list(await session.scalars(select(DocumentSnapshot)))
    stale = [
        s
        for s in snapshots
        if s.snapshot_id not in active
        and s.created_at < cutoff
        and s.payload.get("job_id") not in running
    ]
    print(f"Unreachable snapshots older than 24h: {len(stale)}")
    if execute:
        for snapshot in stale:
            store = MilvusKnowledgeStore(
                get_settings().model_copy(
                    update={"milvus_collection": snapshot.collection}
                )
            )
            try:
                if await asyncio.to_thread(
                    store.client.has_collection, snapshot.collection
                ):
                    ids = [c["chunk_id"] for c in snapshot.payload["children"]]
                    for start in range(0, len(ids), 500):
                        await asyncio.to_thread(
                            store.client.delete,
                            collection_name=snapshot.collection,
                            ids=ids[start : start + 500],
                        )
            finally:
                store.close()
            async with session_factory() as session:
                row = await session.get(DocumentSnapshot, snapshot.snapshot_id)
                if row:
                    await session.delete(row)
                await session.commit()
    # Re-read references after cleanup; recent uploads and active jobs are never candidates.
    async with session_factory() as session:
        snapshots = list(await session.scalars(select(DocumentSnapshot)))
        keys = {s.payload.get("object_key") for s in snapshots}
        keys.update(
            await session.scalars(
                select(IndexingJob.source_path).where(
                    IndexingJob.status.in_(["pending", "running"])
                )
            )
        )
    objects = ObjectStore()
    pages = objects.client.get_paginator("list_objects_v2").paginate(
        Bucket=settings().object_bucket, Prefix="uploads/"
    )
    count = 0
    for page in pages:
        for obj in page.get("Contents", []):
            if (
                obj["Key"] not in keys
                and obj["LastModified"].astimezone(UTC).replace(tzinfo=None) < cutoff
            ):
                count += 1
                if execute:
                    await asyncio.to_thread(objects.delete, obj["Key"])
    print(f"Unreferenced upload objects older than 24h: {count}; execute={execute}")
    await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    asyncio.run(reconcile(parser.parse_args().execute))
