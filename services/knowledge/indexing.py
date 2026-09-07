import asyncio
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from sqlalchemy import select
from app.config import get_settings
from app.ingestion import parse_source
from app.rag.documents import chunk_documents
from app.rag.milvus_store import MilvusKnowledgeStore
from packages.platform.client import ServiceClient
from packages.platform.tasks import current_lease, guard, now
from services.knowledge.db import session_factory
from services.knowledge.models import IndexingJob, KnowledgeState, DocumentHead, DocumentSnapshot
from services.knowledge.objects import ObjectStore


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


async def index_job(job, rebuild=None):
    config = get_settings()
    async with session_factory() as session:
        await guard(session)
        record = await session.get(IndexingJob, job.job_id)
        if record.status == "completed":
            return
        record.status, record.started_at = "running", now()
        state = await session.get(KnowledgeState, 1)
        epoch, collection = state.epoch, state.collection
        await session.commit()
    with TemporaryDirectory(prefix="evidence-ingest-") as temporary:
        path = Path(temporary) / ("source" + Path(job.original_filename or job.source_path).suffix)
        await asyncio.to_thread(ObjectStore().download, job.source_path, path, job.content_sha256)
        documents = await asyncio.wait_for(asyncio.to_thread(parse_source, path,
            original_filename=job.original_filename or path.name,
            pdf_ocr_enabled=config.pdf_ocr_enabled, pdf_ocr_language=config.pdf_ocr_language,
            pdf_ocr_dpi=config.pdf_ocr_dpi, excel_rows_per_block=config.excel_rows_per_block),
            timeout=config.knowledge_upload_parse_timeout_seconds)
    # Reserve revisions before doing expensive inference. Deletion or a newer writer supersedes this attempt.
    revisions = {}
    async with session_factory() as session:
        await guard(session)
        state = await session.scalar(select(KnowledgeState).where(KnowledgeState.state_id == 1).with_for_update())
        if state.epoch != epoch:
            raise RuntimeError("index generation changed; retry required")
        for document in sorted(documents, key=lambda item: item.document_id):
            head = await session.get(DocumentHead, document.document_id)
            if head is None:
                head = DocumentHead(document_id=document.document_id, revision=0, updated_at=now())
                session.add(head)
            if head.deleted_at and head.deleted_at >= job.created_at:
                raise RuntimeError("document was deleted after this upload was submitted")
            head.revision += 1
            revisions[document.document_id] = head.revision
        await session.commit()
    if rebuild is not None:
        collection = rebuild["collection"]
    elif job.recreate:
        collection = config.milvus_collection + "_" + uuid4().hex[:16]
    store = MilvusKnowledgeStore(config.model_copy(update={"milvus_collection": collection}))
    snapshots = []
    try:
        await asyncio.to_thread(store.ensure_collection, recreate=False)
        for document in documents:
            chunks = chunk_documents([document], parent_chunk_size=config.knowledge_parent_chunk_size,
                parent_overlap=config.knowledge_parent_chunk_overlap, child_chunk_size=config.knowledge_child_chunk_size,
                child_overlap=config.knowledge_child_chunk_overlap)
            snapshot_id = digest(f"{job.job_id}:{current_lease.get().generation}:{document.document_id}")
            mapping = {parent.parent_chunk_id: digest(snapshot_id+parent.parent_chunk_id) for parent in chunks.parents}
            parents = [parent.model_copy(update={"parent_chunk_id": mapping[parent.parent_chunk_id]}) for parent in chunks.parents]
            children = [child.model_copy(update={"chunk_id": digest(snapshot_id+child.chunk_id),
                "parent_chunk_id": mapping[child.parent_chunk_id]}) for child in chunks.children]
            payload = {"document": document.model_dump(mode="json"),
                "parents": [p.model_dump(mode="json") for p in parents],
                "children": [c.model_dump(mode="json") for c in children], "object_key": job.source_path, "job_id": job.job_id}
            # Snapshot is persisted before vectors. Only the head pointer makes it visible.
            async with session_factory() as session:
                await guard(session)
                session.add(DocumentSnapshot(snapshot_id=snapshot_id, document_id=document.document_id,
                    collection=collection, payload=payload, created_at=now()))
                await session.commit()
            for start in range(0, len(children), min(config.knowledge_index_batch_size, 16)):
                batch = children[start:start+min(config.knowledge_index_batch_size,16)]
                vectors = await ServiceClient("inference").post("/internal/v1/embed",
                    {"texts": [child.embedding_text for child in batch], "priority": 1})
                await asyncio.to_thread(store.upsert_chunks, batch, vectors["vectors"])
            snapshots.append((document.document_id, snapshot_id, len(children)))
        if rebuild is not None:
            rebuild["jobs"].append((job.job_id, snapshots, revisions, job.created_at))
            return
        async with session_factory() as session:
            await guard(session)
            state = await session.scalar(select(KnowledgeState).where(KnowledgeState.state_id == 1).with_for_update())
            if state.epoch != epoch:
                raise RuntimeError("index generation changed before publication")
            for document_id, snapshot_id, _ in snapshots:
                head = await session.get(DocumentHead, document_id)
                if head.revision != revisions[document_id] or (head.deleted_at and head.deleted_at >= job.created_at):
                    raise RuntimeError("document revision superseded")
            if job.recreate:
                heads = list(await session.scalars(select(DocumentHead)))
                for head in heads:
                    head.active_snapshot = None
                state.collection, state.epoch = collection, state.epoch+1
            else:
                state.epoch += 1
            for document_id, snapshot_id, _ in snapshots:
                head = await session.get(DocumentHead, document_id)
                head.active_snapshot, head.deleted_at, head.updated_at = snapshot_id, None, now()
            record = await session.get(IndexingJob, job.job_id)
            record.status, record.completed_at, record.error_message = "completed", now(), None
            record.document_count, record.chunk_count = len(snapshots), sum(item[2] for item in snapshots)
            await session.commit()
    finally:
        store.close()


async def rebuild_batch(jobs):
    async with session_factory() as session:
        state = await session.get(KnowledgeState, 1)
        epoch = state.epoch
    staged = {"collection": get_settings().milvus_collection+"_"+uuid4().hex[:16], "jobs": []}
    for job in jobs:
        await index_job(job, staged)
    async with session_factory() as session:
        await guard(session)
        state = await session.scalar(select(KnowledgeState).where(KnowledgeState.state_id == 1).with_for_update())
        if state.epoch != epoch:
            raise RuntimeError("knowledge changed during full rebuild")
        latest = {}
        for _, snapshots, revisions, created in staged["jobs"]:
            for document_id, snapshot_id, _ in snapshots:
                latest[document_id] = (snapshot_id, revisions[document_id], created)
        for document_id, (_, revision, created) in latest.items():
            head = await session.get(DocumentHead, document_id)
            if head.revision != revision or (head.deleted_at and head.deleted_at >= created):
                raise RuntimeError("rebuild was superseded by a document change")
        for head in list(await session.scalars(select(DocumentHead))):
            if head.document_id in latest:
                head.active_snapshot = latest[head.document_id][0]
                head.deleted_at = None
            else:
                head.active_snapshot = None
            head.updated_at = now()
        state.collection, state.epoch = staged["collection"], epoch+1
        for job_id, snapshots, _, _ in staged["jobs"]:
            job = await session.get(IndexingJob, job_id)
            job.status, job.completed_at, job.error_message = "completed", now(), None
            job.document_count, job.chunk_count = len(snapshots), sum(item[2] for item in snapshots)
        await session.commit()
