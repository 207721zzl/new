from types import SimpleNamespace
from uuid import uuid4
import pytest
from sqlalchemy import select
from packages.platform.tasks import now
from services.knowledge.db import session_factory
from services.knowledge.models import DocumentHead, DocumentSnapshot, DurableTask
from services.knowledge.store import IndexingJobStore
from services.knowledge.worker import runner
from services.knowledge.retrieval import retrieve
from services.knowledge.deletion import delete_document
from services.knowledge.objects import ObjectStore
from packages.platform.client import ServiceClient
from app.rag.models import RetrievalHit


@pytest.fixture
def backends(monkeypatch, tmp_path):
    objects = {}
    vectors = {}

    def upload(self, path, key):
        objects[key] = path.read_bytes()

    def download(self, key, path, expected_hash=None):
        path.write_bytes(objects[key])

    def delete(self, key):
        objects.pop(key, None)

    monkeypatch.setattr(ObjectStore, "upload", upload)
    monkeypatch.setattr(ObjectStore, "download", download)
    monkeypatch.setattr(ObjectStore, "delete", delete)

    async def post(self, path, payload, headers=None):
        if path.endswith("/embed"):
            return {"vectors": [[0.0] * 1024 for _ in payload["texts"]]}
        if path.endswith("/rerank"):
            return {
                "results": [
                    {"original_index": i, "score": 0.9, "content": d}
                    for i, d in enumerate(payload["documents"])
                ]
            }
        return {"status": "recorded"}

    monkeypatch.setattr(ServiceClient, "post", post)

    class Vectors:
        def __init__(self, settings):
            self.collection = settings.milvus_collection
            self.client = self

        def ensure_collection(self, recreate=False):
            vectors.setdefault(self.collection, {})

        def upsert_chunks(self, chunks, dense):
            for chunk in chunks:
                vectors[self.collection][chunk.chunk_id] = chunk

        def hybrid_search(self, query, dense, limit):
            return [
                RetrievalHit(
                    **{
                        key: (
                            getattr(c, key).isoformat()
                            if key == "updated_at"
                            else getattr(c, key)
                        )
                        for key in RetrievalHit.__dataclass_fields__
                        if key != "retrieval_score"
                    },
                    retrieval_score=0.8,
                )
                for c in list(vectors[self.collection].values())[:limit]
            ]

        def has_collection(self, name):
            return name in vectors

        def delete(self, collection_name, ids):
            for key in ids:
                vectors[collection_name].pop(key, None)

        def close(self):
            pass

    for module in ["indexing", "retrieval", "deletion"]:
        monkeypatch.setattr(
            f"services.knowledge.{module}.MilvusKnowledgeStore", Vectors
        )
    return objects, vectors


async def submit(
    tmp_path, content="报销需要主管审批。", name="policy.txt", recreate=False
):
    import hashlib

    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    batch = SimpleNamespace(
        batch_id=str(uuid4()),
        files=[
            SimpleNamespace(
                source_path=path,
                original_filename=name,
                file_size_bytes=path.stat().st_size,
                content_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        ],
    )
    jobs = await IndexingJobStore().create_batch(
        batch, created_by_user_id="admin", recreate=recreate
    )
    await runner().execute(batch.batch_id)
    return batch, jobs


@pytest.mark.asyncio
async def test_upload_index_retrieve_delete_end_to_end(databases, backends, tmp_path):
    batch, jobs = await submit(tmp_path)
    job = await IndexingJobStore().get(jobs[0].job_id)
    assert job.status == "completed", job.error_message
    hits = await retrieve("报销需要什么", 5)
    assert len(hits) == 1 and "主管审批" in hits[0].context_content
    await runner().execute(batch.batch_id)
    async with session_factory() as session:
        assert len(list(await session.scalars(select(DocumentSnapshot)))) == 1
    result = await delete_document(hits[0].hit.document_id, "admin")
    assert result.status == "deleted" and result.child_chunk_count == 1
    assert await retrieve("报销需要什么", 5) == []
    async with session_factory() as session:
        tasks = list(
            await session.scalars(
                select(DurableTask).where(DurableTask.kind == "delete")
            )
        )
    await runner().execute(tasks[0].task_id)
    assert sum(len(rows) for rows in backends[1].values()) == 0


@pytest.mark.asyncio
async def test_new_version_publishes_only_after_vectors_succeed(
    databases, backends, tmp_path, monkeypatch
):
    await submit(tmp_path)
    initial = (await retrieve("报销", 5))[0]
    original = ServiceClient.post

    async def fail(self, path, payload, headers=None):
        if path.endswith("/embed") and payload.get("priority") == 1:
            raise ConnectionError("inference unavailable")
        return await original(self, path, payload, headers)

    monkeypatch.setattr(ServiceClient, "post", fail)
    await submit(tmp_path, "报销新规定需要财务审批。")
    found = await retrieve("报销", 5)
    assert found[0].hit.chunk_id == initial.hit.chunk_id
    assert "主管审批" in found[0].context_content


@pytest.mark.asyncio
async def test_deleted_document_is_not_resurrected_by_inflight_upload(
    databases, backends, tmp_path, monkeypatch
):
    await submit(tmp_path)
    document_id = (await retrieve("报销", 5))[0].hit.document_id
    original = ServiceClient.post
    deleted = False

    async def delete_during_embed(self, path, payload, headers=None):
        nonlocal deleted
        if path.endswith("/embed") and payload.get("priority") == 1 and not deleted:
            deleted = True
            await delete_document(document_id, "admin")
        return await original(self, path, payload, headers)

    monkeypatch.setattr(ServiceClient, "post", delete_during_embed)
    await submit(tmp_path, "报销新规定需要财务审批。")
    async with session_factory() as session:
        head = await session.get(DocumentHead, document_id)
        assert head.active_snapshot is None and head.deleted_at is not None
    assert await retrieve("报销", 5) == []


@pytest.mark.asyncio
async def test_rebuild_batch_does_not_publish_half_finished_collection(
    databases, backends, tmp_path, monkeypatch
):
    import hashlib

    await submit(tmp_path, name="old.txt")
    initial = (await retrieve("报销", 5))[0].hit.document_id
    files = []
    for name, content in [
        ("first.txt", "第一篇文档内容。"),
        ("second.txt", "第二篇文档内容。"),
    ]:
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        files.append(
            SimpleNamespace(
                source_path=path,
                original_filename=name,
                file_size_bytes=path.stat().st_size,
                content_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    batch = SimpleNamespace(batch_id=str(uuid4()), files=files)
    await IndexingJobStore().create_batch(
        batch, created_by_user_id="admin", recreate=True
    )
    original = ServiceClient.post
    count = 0

    async def failure(self, path, payload, headers=None):
        nonlocal count
        if path.endswith("/embed") and payload.get("priority") == 1:
            count += 1
            if count == 2:
                raise ConnectionError("second file fails")
        return await original(self, path, payload, headers)

    monkeypatch.setattr(ServiceClient, "post", failure)
    await runner().execute(batch.batch_id)
    assert (await retrieve("报销", 5))[0].hit.document_id == initial
    monkeypatch.setattr(ServiceClient, "post", original)
    from datetime import timedelta

    async with session_factory() as session:
        task = await session.get(DurableTask, batch.batch_id)
        task.available_at = now() - timedelta(seconds=1)
        await session.commit()
    await runner().execute(batch.batch_id)
    documents = await IndexingJobStore().list_documents(limit=20, offset=0)
    assert len(documents) == 2
    assert initial not in {d["document_id"] for d in documents}
