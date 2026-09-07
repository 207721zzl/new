"""Conversation persistence ordering regression tests."""

import asyncio

from app.db.repositories import ConversationRepository


class RecordingSession:
    def __init__(self) -> None:
        self.added = []
        self.events: list[str] = []

    def add(self, value) -> None:
        self.added.append(value)
        self.events.append(f"add:{value.__class__.__name__}")

    async def flush(self) -> None:
        names = ",".join(value.__class__.__name__ for value in self.added)
        self.events.append(f"flush:{names}")

    async def commit(self) -> None:
        self.events.append("commit")


def test_new_conversation_is_flushed_before_run_and_message() -> None:
    session = RecordingSession()

    run = asyncio.run(
        ConversationRepository(session).create_turn(
            user_id="user-1",
            requested_conversation_id=None,
            message_id="message-1",
            run_id="run-1",
            question="退款率怎么计算？",
            top_k=5,
            initial_event={"sequence": 1},
        )
    )

    assert run.conversation_id == "run-1"
    assert session.added[0].user_id == "user-1"
    assert session.events == [
        "add:Conversation",
        "flush:Conversation",
        "add:AgentRun",
        "add:Message",
        "commit",
    ]
