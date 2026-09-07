"""多轮会话的轮次分配、并发保护和上下文预算测试。"""

import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db.models import Conversation
from app.db.repositories import ConversationRepository
from app.errors import ConversationBusyError
from app.runs.service import AgentRunStore


class FakeSession:
    def __init__(self, scalar_results):
        self.scalar_results = list(scalar_results)
        self.added = []
        self.committed = False

    async def scalar(self, statement):
        return self.scalar_results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


def test_existing_conversation_allocates_next_turn_under_lock():
    conversation = Conversation(conversation_id="conversation-1", title="首轮")
    session = FakeSession([conversation, None, 2])

    run = asyncio.run(
        ConversationRepository(session).create_turn(
            requested_conversation_id="conversation-1",
            message_id="message-3",
            run_id="run-3",
            question="那华南呢？",
            top_k=None,
            initial_event={"sequence": 1},
        )
    )

    assert run.conversation_id == "conversation-1"
    assert run.turn_index == 3
    assert session.committed is True
    assert [item.__class__.__name__ for item in session.added] == ["AgentRun", "Message"]


def test_conversation_rejects_a_second_active_turn():
    conversation = Conversation(conversation_id="conversation-1", title="首轮")
    session = FakeSession([conversation, "active-run"])

    with pytest.raises(ConversationBusyError):
        asyncio.run(
            ConversationRepository(session).create_turn(
                requested_conversation_id="conversation-1",
                message_id="message-2",
                run_id="run-2",
                question="那华南呢？",
                top_k=None,
                initial_event={"sequence": 1},
            )
        )


class FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeContextRepository:
    def __init__(self, session):
        pass

    async def list_context_messages(
        self,
        conversation_id,
        *,
        exclude_run_id,
        limit,
    ):
        assert conversation_id == "conversation-1"
        assert exclude_run_id == "run-2"
        assert limit == 4
        return [
            SimpleNamespace(role="user", content="用" * 1000),
            SimpleNamespace(role="assistant", content="答" * 1000),
        ]


def test_context_budget_keeps_latest_user_and_assistant_messages(monkeypatch):
    monkeypatch.setattr(
        "app.runs.service.ConversationRepository",
        FakeContextRepository,
    )
    store = AgentRunStore(
        session_factory=lambda: FakeSessionContext(),
        settings=Settings(
            _env_file=None,
            conversation_recent_turns=2,
            conversation_context_max_chars=500,
        ),
    )

    context = asyncio.run(
        store.get_context("conversation-1", exclude_run_id="run-2")
    )

    assert [message["role"] for message in context] == ["user", "assistant"]
    assert sum(len(message["content"]) for message in context) == 500
