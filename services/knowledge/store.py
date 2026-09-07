import asyncio
import hashlib
from uuid import uuid4
from sqlalchemy import select
from app.config import PROJECT_ROOT, get_settings
from app.errors import IndexJobNotFoundError, KnowledgeUploadBatchNotFoundError, KnowledgeIndexRequestError
from services.knowledge.db import session_factory
from services.knowledge.models import IndexingJob, DurableTask, DocumentHead, DocumentSnapshot
from services.knowledge.objects import ObjectStore
from packages.platform.tasks import enqueue


class IndexingJobStore:
    def __init__(self, **kwargs):
        self.sessions = kwargs.get("session_factory") or session_factory
        self.objects = ObjectStore()

    async def create_batch(self, batch, *, created_by_user_id, recreate):
        objects, jobs = [], []
        commit_attempted = False
        try:
            for index, item in enumerate(batch.files):
                key = f"uploads/{batch.batch_id}/{uuid4().hex}{item.source_path.suffix}"
                await asyncio.to_thread(self.objects.upload, item.source_path, key)
                objects.append(key)
                jobs.append(IndexingJob(job_id=str(uuid4()), created_by_user_id=created_by_user_id,
                    batch_id=batch.batch_id, source_path=key, original_filename=item.original_filename,
                    file_size_bytes=item.file_size_bytes, content_sha256=item.content_sha256,
                    recreate=recreate and index == 0, status="pending", document_count=0, chunk_count=0))
            async with self.sessions() as session:
                session.add_all(jobs)
                # One batch task preserves recreate-first ordering. Completed files are skipped on retry.
                enqueue(session, DurableTask, "index_batch", batch.batch_id, task_id=batch.batch_id)
                commit_attempted = True
                await session.commit()
            return jobs
        except Exception:
            # A lost commit response is ambiguous. Keep the source for reconciliation.
            for key in ([] if commit_attempted else objects):
                try:
                    await asyncio.to_thread(self.objects.delete, key)
                except Exception:
                    pass  # The reconciliation job removes old unreferenced objects.
            raise

    async def create(self, *, created_by_user_id, source_file, recreate):
        from types import SimpleNamespace
        allowed = (PROJECT_ROOT / "data" / "knowledge").resolve()
        path = (allowed / source_file).resolve() if source_file else get_settings().resolved_knowledge_source_file.resolve()
        if not path.is_relative_to(allowed) or not path.is_file():
            raise KnowledgeIndexRequestError()
        batch = SimpleNamespace(batch_id=str(uuid4()), files=[SimpleNamespace(source_path=path,
            original_filename=path.name, file_size_bytes=path.stat().st_size,
            content_sha256=hashlib.sha256(path.read_bytes()).hexdigest())])
        return (await self.create_batch(batch, created_by_user_id=created_by_user_id, recreate=recreate))[0]

    async def get(self, job_id):
        async with self.sessions() as session:
            job = await session.get(IndexingJob, job_id)
        if job is None:
            raise IndexJobNotFoundError()
        return job

    async def get_batch(self, batch_id):
        async with self.sessions() as session:
            jobs = list(await session.scalars(select(IndexingJob).where(IndexingJob.batch_id == batch_id)
                .order_by(IndexingJob.recreate.desc(), IndexingJob.created_at, IndexingJob.job_id)))
        if not jobs:
            raise KnowledgeUploadBatchNotFoundError()
        return jobs

    async def list_documents(self, *, limit, offset):
        async with self.sessions() as session:
            rows = (await session.execute(select(DocumentHead, DocumentSnapshot).join(
                DocumentSnapshot, DocumentHead.active_snapshot == DocumentSnapshot.snapshot_id)
                .where(DocumentHead.deleted_at.is_(None)).order_by(DocumentHead.updated_at.desc())
                .offset(offset).limit(limit))).all()
        return [{**{key: snapshot.payload["document"].get(key) for key in ["document_id", "title", "doc_type",
                "source", "version", "original_filename", "mime_type", "language"]},
                "index_status": "completed", "parent_chunk_count": len(snapshot.payload["parents"]),
                "child_chunk_count": len(snapshot.payload["children"]), "updated_at": head.updated_at}
                for head, snapshot in rows]
