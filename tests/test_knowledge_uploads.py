"""知识文件批量上传、落盘安全和聚合状态测试。"""

import asyncio
import hashlib
import json
import shutil
from datetime import UTC, date, datetime
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.errors import KnowledgeUploadRequestError, KnowledgeUploadTooLargeError
from app.main import app, get_indexing_job_store
from app.operations.service import IndexingJobStore
from app.operations.uploads import discard_staged_batch, stage_knowledge_uploads
from app.rag.models import KnowledgeChunk


client = TestClient(app)


def knowledge_json(document_id: str) -> bytes:
    return json.dumps(
        [
            {
                "document_id": document_id,
                "title": f"{document_id} 标题",
                "content": f"{document_id} 正文",
                "doc_type": "metric_definition",
                "metric_name": document_id,
                "source": "上传测试",
                "version": "1.0",
                "updated_at": "2026-07-19",
            }
        ],
        ensure_ascii=False,
    ).encode("utf-8")


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def workspace_tmp_path():
    path = PROJECT_ROOT / ".codex_tmp" / f"knowledge-upload-{uuid4().hex}"
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path)


def test_stage_multiple_json_files_with_randomized_storage_names(
    workspace_tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("app.operations.uploads.PROJECT_ROOT", workspace_tmp_path)
    settings = Settings(_env_file=None)
    first = knowledge_json("metric_a")
    second = knowledge_json("metric_b")

    batch = asyncio.run(
        stage_knowledge_uploads(
            [
                UploadFile(filename="指标 A.json", file=BytesIO(first)),
                UploadFile(filename="../指标 B.json", file=BytesIO(second)),
            ],
            settings,
        )
    )

    assert [item.original_filename for item in batch.files] == [
        "指标 A.json",
        "指标 B.json",
    ]
    assert batch.files[0].source_path.name != "指标 A.json"
    assert batch.files[0].source_path.parent == batch.directory
    assert batch.files[0].content_sha256 == hashlib.sha256(first).hexdigest()
    assert batch.files[1].file_size_bytes == len(second)

    discard_staged_batch(batch)
    assert not batch.directory.exists()


def test_stage_rejects_invalid_type_and_rolls_back_batch(
    workspace_tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("app.operations.uploads.PROJECT_ROOT", workspace_tmp_path)
    settings = Settings(_env_file=None)

    with pytest.raises(KnowledgeUploadRequestError):
        asyncio.run(
            stage_knowledge_uploads(
                [UploadFile(filename="notes.txt", file=BytesIO(b"not json"))],
                settings,
            )
        )

    upload_root = workspace_tmp_path / "data" / "knowledge" / "uploads"
    assert upload_root.exists()
    assert list(upload_root.iterdir()) == []


def test_stage_rolls_back_all_files_when_one_json_is_invalid(
    workspace_tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("app.operations.uploads.PROJECT_ROOT", workspace_tmp_path)
    settings = Settings(_env_file=None)

    with pytest.raises(KnowledgeUploadRequestError):
        asyncio.run(
            stage_knowledge_uploads(
                [
                    UploadFile(
                        filename="valid.json",
                        file=BytesIO(knowledge_json("metric_valid")),
                    ),
                    UploadFile(filename="broken.json", file=BytesIO(b"{")),
                ],
                settings,
            )
        )

    upload_root = workspace_tmp_path / "data" / "knowledge" / "uploads"
    assert list(upload_root.iterdir()) == []


def test_stage_enforces_file_size_and_rolls_back(workspace_tmp_path, monkeypatch):
    monkeypatch.setattr("app.operations.uploads.PROJECT_ROOT", workspace_tmp_path)
    settings = Settings(
        _env_file=None,
        knowledge_upload_max_file_bytes=1024,
        knowledge_upload_max_batch_bytes=1024,
    )

    with pytest.raises(KnowledgeUploadTooLargeError):
        asyncio.run(
            stage_knowledge_uploads(
                [UploadFile(filename="large.json", file=BytesIO(b"x" * 1025))],
                settings,
            )
        )

    upload_root = workspace_tmp_path / "data" / "knowledge" / "uploads"
    assert list(upload_root.iterdir()) == []


class FakeBatchIndexingStore:
    def __init__(self):
        self.jobs = []
        self.executed_batch_id = None

    async def create_batch(self, batch, *, recreate):
        assert recreate is False
        now = datetime.now(UTC)
        self.jobs = [
            SimpleNamespace(
                job_id=f"job-{index}",
                batch_id=batch.batch_id,
                source_path=str(item.source_path),
                original_filename=item.original_filename,
                file_size_bytes=item.file_size_bytes,
                status="pending",
                recreate=False,
                document_count=0,
                chunk_count=0,
                created_at=now,
                started_at=None,
                completed_at=None,
            )
            for index, item in enumerate(batch.files, start=1)
        ]
        return self.jobs

    async def execute_batch(self, batch_id):
        self.executed_batch_id = batch_id
        now = datetime.now(UTC)
        for job in self.jobs:
            job.status = "completed"
            job.started_at = now
            job.completed_at = now
            job.document_count = 1
            job.chunk_count = 2

    async def get_batch(self, batch_id):
        assert batch_id == self.jobs[0].batch_id
        return self.jobs


def test_batch_upload_endpoint_creates_jobs_and_exposes_aggregate_status(
    workspace_tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("app.operations.uploads.PROJECT_ROOT", workspace_tmp_path)
    store = FakeBatchIndexingStore()
    app.dependency_overrides[get_indexing_job_store] = lambda: store

    response = client.post(
        "/api/v1/knowledge/uploads",
        files=[
            ("files", ("one.json", knowledge_json("metric_one"), "application/json")),
            ("files", ("two.json", knowledge_json("metric_two"), "application/json")),
        ],
        data={"recreate": "false"},
    )

    assert response.status_code == 202
    created = response.json()
    assert [item["filename"] for item in created["items"]] == [
        "one.json",
        "two.json",
    ]
    assert store.executed_batch_id == created["batch_id"]

    detail = client.get(created["status_url"])
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"
    assert detail.json()["file_count"] == 2
    assert detail.json()["document_count"] == 2
    assert detail.json()["chunk_count"] == 4


def test_batch_upload_endpoint_rejects_non_json_before_creating_jobs(
    workspace_tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("app.operations.uploads.PROJECT_ROOT", workspace_tmp_path)
    store = FakeBatchIndexingStore()
    app.dependency_overrides[get_indexing_job_store] = lambda: store

    response = client.post(
        "/api/v1/knowledge/uploads",
        files=[("files", ("notes.txt", b"plain text", "text/plain"))],
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "knowledge_upload_request_invalid"
    assert store.jobs == []


def test_vector_writes_are_split_into_configured_batches(monkeypatch):
    class FakeEmbedder:
        def __init__(self):
            self.batch_lengths = []

        def encode(self, texts):
            self.batch_lengths.append(len(texts))
            return [[float(index)] for index, _ in enumerate(texts)]

    class FakeMilvusStore:
        def __init__(self):
            self.ensure_calls = []
            self.upsert_lengths = []
            self.closed = False

        def ensure_collection(self, recreate=False):
            self.ensure_calls.append(recreate)

        def upsert_chunks(self, chunks, vectors):
            assert len(chunks) == len(vectors)
            self.upsert_lengths.append(len(chunks))

        def close(self):
            self.closed = True

    embedder = FakeEmbedder()
    milvus = FakeMilvusStore()
    monkeypatch.setattr("app.operations.service.get_embedder", lambda: embedder)
    monkeypatch.setattr("app.operations.service.MilvusKnowledgeStore", lambda _: milvus)
    settings = Settings(_env_file=None, knowledge_index_batch_size=2)
    store = IndexingJobStore(settings)
    chunks = [
        KnowledgeChunk(
            chunk_id=str(index).zfill(64),
            parent_chunk_id=str(index + 10).zfill(64),
            document_id=f"document-{index}",
            title="标题",
            content="正文",
            parent_index=0,
            chunk_index=0,
            doc_type="metric_definition",
            metric_name="metric",
            source="test",
            version="1.0",
            updated_at=date(2026, 7, 19),
        )
        for index in range(5)
    ]

    store._write_vectors(chunks, recreate=True)

    assert embedder.batch_lengths == [2, 2, 1]
    assert milvus.ensure_calls == [True]
    assert milvus.upsert_lengths == [2, 2, 1]
    assert milvus.closed is True
