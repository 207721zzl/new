import asyncio
from uuid import uuid4
from sqlalchemy import select
from app.admin.schemas import KnowledgeDocumentDeleted
from app.config import get_settings
from app.errors import KnowledgeDocumentNotFoundError
from app.rag.milvus_store import MilvusKnowledgeStore
from services.knowledge.db import session_factory
from services.knowledge.models import KnowledgeState, DocumentHead, DocumentSnapshot, KnowledgeAudit, DurableTask
from packages.platform.tasks import enqueue, guard, now


async def delete_document(document_id, actor):
    async with session_factory() as session:
        state = await session.scalar(select(KnowledgeState).where(KnowledgeState.state_id == 1).with_for_update())
        state.epoch += 1
        head = await session.get(DocumentHead, document_id)
        if head is None or head.active_snapshot is None or head.deleted_at:
            raise KnowledgeDocumentNotFoundError()
        active = await session.get(DocumentSnapshot, head.active_snapshot)
        snapshots = list(await session.scalars(select(DocumentSnapshot.snapshot_id).where(
            DocumentSnapshot.document_id == document_id)))
        audit_id = str(uuid4())
        head.active_snapshot, head.deleted_at, head.updated_at = None, now(), now()
        head.revision += 1
        session.add(KnowledgeAudit(audit_id=audit_id, actor_user_id=actor,
            action="knowledge.document_deleted", document_id=document_id,
            details={"snapshot_ids": snapshots}, created_at=now()))
        enqueue(session, DurableTask, "delete", audit_id)
        enqueue(session, DurableTask, "audit", audit_id)
        await session.commit()
    # Public deletion succeeds at logical removal; queued physical cleanup is recoverable.
    return KnowledgeDocumentDeleted(document_id=document_id, title=active.payload["document"]["title"],
        parent_chunk_count=len(active.payload["parents"]), child_chunk_count=len(active.payload["children"]))


async def cleanup(audit_id):
    async with session_factory() as session:
        audit = await session.get(KnowledgeAudit, audit_id)
        ids = audit.details["snapshot_ids"]
    for snapshot_id in ids:
        async with session_factory() as session:
            snapshot = await session.get(DocumentSnapshot, snapshot_id)
            if snapshot is None:
                continue
        store = MilvusKnowledgeStore(get_settings().model_copy(update={"milvus_collection": snapshot.collection}))
        try:
            if await asyncio.to_thread(store.client.has_collection, snapshot.collection):
                chunk_ids = [c["chunk_id"] for c in snapshot.payload["children"]]
                for start in range(0, len(chunk_ids), 500):
                    await asyncio.to_thread(store.client.delete, collection_name=snapshot.collection, ids=chunk_ids[start:start+500])
        finally:
            store.close()
        async with session_factory() as session:
            await guard(session)
            snapshot = await session.get(DocumentSnapshot, snapshot_id)
            if snapshot:
                await session.delete(snapshot)
            await session.commit()
