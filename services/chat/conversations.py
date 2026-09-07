from typing import Any
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from services.chat.db import get_admin_session_factory
from services.chat.repositories import ConversationRepository, FeedbackRepository
class ConversationStore:
    """读取持久化会话及其消息。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.session_factory = session_factory or get_admin_session_factory()

    async def list_recent(
        self,
        *,
        user_id: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            repository = ConversationRepository(session)
            conversations = await repository.list_recent(
                user_id=user_id,
                limit=limit,
                offset=offset,
            )
            result: list[dict[str, Any]] = []
            for conversation in conversations:
                messages = await repository.list_messages(
                    conversation.conversation_id,
                    user_id=user_id,
                )
                result.append(self._serialize(conversation, messages))
            return result

    async def get(
        self,
        conversation_id: str,
        *,
        user_id: str,
    ) -> dict[str, Any] | None:
        """读取一个会话的全部持久化消息。"""
        async with self.session_factory() as session:
            repository = ConversationRepository(session)
            conversation = await repository.get_for_user(conversation_id, user_id)
            if conversation is None:
                return None
            messages = await repository.list_messages(
                conversation_id,
                user_id=user_id,
            )
            return self._serialize(conversation, messages)

    @staticmethod
    def _serialize(conversation, messages) -> dict[str, Any]:
        latest_run_id = messages[-1].run_id if messages else None
        return {
            "conversation_id": conversation.conversation_id,
            # 保留 run_id 字段兼容既有列表消费者，语义调整为最新一轮运行。
            "run_id": latest_run_id,
            "title": conversation.title,
            "messages": [
                {
                    "message_id": message.message_id,
                    "run_id": message.run_id,
                    "role": message.role,
                    "content": message.content,
                    "created_at": message.created_at,
                }
                for message in messages
            ],
            "created_at": conversation.created_at,
            "updated_at": conversation.updated_at,
        }


class FeedbackStore:
    """保存经过运行存在性校验的用户反馈。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.session_factory = session_factory or get_admin_session_factory()

    async def create(
        self,
        *,
        user_id: str,
        run_id: str,
        rating: int,
        comment: str | None,
        correction: str | None,
    ) -> str | None:
        feedback_id = str(uuid4())
        async with self.session_factory() as session:
            feedback = await FeedbackRepository(session).create(
                user_id=user_id,
                feedback_id=feedback_id,
                run_id=run_id,
                rating=rating,
                comment=comment,
                correction=correction,
            )
        return feedback_id if feedback is not None else None
