"""会话、反馈和知识索引 API 的稳定协议测试。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import (
    app,
    get_conversation_store,
    get_feedback_store,
    get_indexing_job_store,
)


client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


class FakeConversationStore:
    async def list_recent(self, *, limit, offset):
        assert limit == 10
        assert offset == 0
        now = datetime.now(UTC)
        return [
            {
                "conversation_id": "conversation-1",
                "run_id": "run-1",
                "title": "上周华东区 GMV 是多少？",
                "messages": [
                    {
                        "message_id": "message-1",
                        "run_id": "run-1",
                        "role": "user",
                        "content": "上周华东区 GMV 是多少？",
                        "created_at": now,
                    }
                ],
                "created_at": now,
                "updated_at": now,
            }
        ]

    async def get(self, conversation_id):
        assert conversation_id == "conversation-1"
        return (await self.list_recent(limit=10, offset=0))[0]


def test_conversation_history_endpoint_returns_messages():
    app.dependency_overrides[get_conversation_store] = FakeConversationStore

    response = client.get("/api/v1/conversations?limit=10")

    assert response.status_code == 200
    assert response.json()["items"][0]["run_id"] == "run-1"
    assert response.json()["items"][0]["messages"][0]["role"] == "user"


def test_conversation_detail_endpoint_returns_complete_thread():
    app.dependency_overrides[get_conversation_store] = FakeConversationStore

    response = client.get("/api/v1/conversations/conversation-1")

    assert response.status_code == 200
    assert response.json()["conversation_id"] == "conversation-1"
    assert response.json()["messages"][0]["run_id"] == "run-1"


class FakeFeedbackStore:
    async def create(self, **kwargs):
        assert kwargs == {
            "run_id": "run-1",
            "rating": -1,
            "comment": "口径需要解释",
            "correction": "应排除取消订单",
        }
        return "feedback-1"


def test_feedback_endpoint_persists_rating_and_correction():
    app.dependency_overrides[get_feedback_store] = FakeFeedbackStore

    response = client.post(
        "/api/v1/feedback",
        json={
            "run_id": "run-1",
            "rating": -1,
            "comment": "口径需要解释",
            "correction": "应排除取消订单",
        },
    )

    assert response.status_code == 201
    assert response.json() == {"feedback_id": "feedback-1", "status": "recorded"}


class MissingRunFeedbackStore:
    async def create(self, **kwargs):
        return None


def test_feedback_for_missing_run_returns_stable_404():
    app.dependency_overrides[get_feedback_store] = MissingRunFeedbackStore

    response = client.post(
        "/api/v1/feedback",
        json={"run_id": "missing", "rating": 1},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "run_not_found"


class FakeIndexingStore:
    def __init__(self):
        now = datetime.now(UTC)
        self.job = SimpleNamespace(
            job_id="job-1",
            source_path="D:/agent项目/data/knowledge/metrics.json",
            status="pending",
            recreate=False,
            document_count=0,
            chunk_count=0,
            created_at=now,
            started_at=None,
            completed_at=None,
        )
        self.executed = False

    async def create(self, *, source_file, recreate):
        assert source_file == "metrics.json"
        assert recreate is False
        return self.job

    async def execute(self, job_id):
        assert job_id == "job-1"
        self.executed = True
        self.job.status = "completed"
        self.job.document_count = 5
        self.job.chunk_count = 8
        self.job.completed_at = datetime.now(UTC)

    async def get(self, job_id):
        assert job_id == "job-1"
        return self.job


def test_index_endpoint_creates_background_job_and_exposes_status():
    store = FakeIndexingStore()
    app.dependency_overrides[get_indexing_job_store] = lambda: store

    created = client.post(
        "/api/v1/knowledge/index",
        json={"source_file": "metrics.json"},
    )

    assert created.status_code == 202
    assert created.json()["job_id"] == "job-1"
    assert store.executed is True

    detail = client.get(created.json()["status_url"])
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"
    assert detail.json()["chunk_count"] == 8
    assert detail.json()["source_file"] == "metrics.json"
