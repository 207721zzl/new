from packages.platform.tasks import enqueue, guard
from services.chat.models import DurableTask
from datetime import UTC, datetime
from typing import Any
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession
from services.chat.models import Conversation, AgentRun, Message, UserFeedback
from app.errors import ConversationBusyError, ConversationNotFoundError
class AgentRunRepository:
    """持久化后台运行状态、结构化结果和可重放事件。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        run_id: str,
        conversation_id: str,
        turn_index: int,
        question: str,
        top_k: int | None,
        initial_event: dict[str, Any],
    ) -> AgentRun:
        run = AgentRun(
            run_id=run_id,
            conversation_id=conversation_id,
            turn_index=turn_index,
            question=question,
            top_k=top_k,
            status="queued",
            current_node="queued",
            events=[initial_event],
        )
        self.session.add(run)
        await self.session.commit()
        return run

    async def get(self, run_id: str) -> AgentRun | None:
        return await self.session.get(AgentRun, run_id)

    async def get_for_user(self, run_id: str, user_id: str) -> AgentRun | None:
        """只返回属于指定用户会话的运行，避免通过 UUID 越权读取。"""
        return await self.session.scalar(
            select(AgentRun)
            .join(
                Conversation,
                Conversation.conversation_id == AgentRun.conversation_id,
            )
            .where(AgentRun.run_id == run_id, Conversation.user_id == user_id)
        )

    async def mark_running(
        self,
        run_id: str,
        *,
        event: dict[str, Any],
    ) -> AgentRun | None:
        await guard(self.session)
        run = await self.get(run_id)
        if run is None:
            return None
        run.status = "running"
        run.current_node = "workflow"
        run.started_at = datetime.now(UTC).replace(tzinfo=None)
        event = {**event, "sequence": max((int(e["sequence"]) for e in run.events), default=0) + 1}
        run.events = [*run.events, event]
        await self.session.commit()
        return run

    async def record_node(
        self,
        run_id: str,
        *,
        node: str,
        intent: str | None,
        event: dict[str, Any],
    ) -> AgentRun | None:
        await guard(self.session)
        run = await self.get(run_id)
        if run is None:
            return None
        run.current_node = node
        if intent is not None:
            run.intent = intent
        event = {**event, "sequence": max((int(e["sequence"]) for e in run.events), default=0) + 1}
        run.events = [*run.events, event]
        await self.session.commit()
        return run

    async def complete(
        self,
        run_id: str,
        *,
        result: dict[str, Any],
        intent: str,
        event: dict[str, Any],
        commit: bool = True,
    ) -> AgentRun | None:
        await guard(self.session)
        run = await self.get(run_id)
        if run is None:
            return None
        run.status = "completed"
        run.current_node = "completed"
        run.intent = intent
        run.result = result
        run.completed_at = datetime.now(UTC).replace(tzinfo=None)
        event = {**event, "sequence": max((int(e["sequence"]) for e in run.events), default=0) + 1}
        run.events = [*run.events, event]
        if commit:
            await self.session.commit()
        return run

    async def fail(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        event: dict[str, Any],
        commit: bool = True,
    ) -> AgentRun | None:
        await guard(self.session)
        run = await self.get(run_id)
        if run is None:
            return None
        run.status = "failed"
        run.current_node = "failed"
        run.error_code = error_code
        run.error_message = error_message
        run.completed_at = datetime.now(UTC).replace(tzinfo=None)
        event = {**event, "sequence": max((int(e["sequence"]) for e in run.events), default=0) + 1}
        run.events = [*run.events, event]
        if commit:
            await self.session.commit()
        return run


class ConversationRepository:
    """维护每次 RAG 运行对应的会话和消息历史。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_turn(
        self,
        *,
        user_id: str,
        requested_conversation_id: str | None,
        message_id: str,
        run_id: str,
        question: str,
        top_k: int | None,
        initial_event: dict[str, Any],
    ) -> AgentRun:
        """锁定已有会话，并原子创建本轮运行与用户消息。"""
        if requested_conversation_id:
            conversation = await self.session.scalar(
                select(Conversation)
                .where(
                    Conversation.conversation_id == requested_conversation_id,
                    Conversation.user_id == user_id,
                )
                .with_for_update()
            )
            if conversation is None:
                raise ConversationNotFoundError()
            active_run = await self.session.scalar(
                select(AgentRun.run_id).where(
                    AgentRun.conversation_id == requested_conversation_id,
                    AgentRun.status.in_(("queued", "running")),
                )
            )
            if active_run is not None:
                raise ConversationBusyError()
            conversation_id = requested_conversation_id
            latest_turn = await self.session.scalar(
                select(func.max(AgentRun.turn_index)).where(
                    AgentRun.conversation_id == conversation_id
                )
            )
            turn_index = int(latest_turn or 0) + 1
        else:
            conversation_id = run_id
            turn_index = 1
            conversation = Conversation(
                conversation_id=conversation_id,
                user_id=user_id,
                title=question[:256],
            )
            self.session.add(conversation)
            # The models intentionally do not define ORM relationships.  Flush the
            # new parent row explicitly so MySQL never sees the AgentRun before its
            # Conversation when the unit of work is committed.
            await self.session.flush()

        run = AgentRun(
            run_id=run_id,
            conversation_id=conversation_id,
            turn_index=turn_index,
            question=question,
            top_k=top_k,
            status="queued",
            current_node="queued",
            events=[initial_event],
        )
        self.session.add(run)
        self.session.add(
            Message(
                message_id=message_id,
                conversation_id=conversation_id,
                run_id=run_id,
                role="user",
                content=question,
            )
        )
        conversation.updated_at = datetime.now(UTC).replace(tzinfo=None)
        enqueue(self.session, DurableTask, "answer", run_id, task_id=run_id)
        await self.session.commit()
        return run

    async def get(self, conversation_id: str) -> Conversation | None:
        """按主键读取会话。"""
        return await self.session.get(Conversation, conversation_id)

    async def get_for_user(
        self,
        conversation_id: str,
        user_id: str,
    ) -> Conversation | None:
        """读取指定用户拥有的会话，对不存在和越权统一返回空。"""
        return await self.session.scalar(
            select(Conversation).where(
                Conversation.conversation_id == conversation_id,
                Conversation.user_id == user_id,
            )
        )

    async def add_assistant_message(
        self,
        *,
        message_id: str,
        run_id: str,
        content: str,
    ) -> None:
        run = await self.session.get(AgentRun, run_id)
        if run is None:
            return
        conversation = await self.session.get(Conversation, run.conversation_id)
        if conversation is None:
            return
        existing = await self.session.scalar(
            select(Message).where(
                Message.run_id == run_id,
                Message.role == "assistant",
            )
        )
        if existing is None:
            self.session.add(
                Message(
                    message_id=message_id,
                    conversation_id=conversation.conversation_id,
                    run_id=run_id,
                    role="assistant",
                    content=content,
                )
            )
        conversation.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await self.session.commit()

    async def list_recent(
        self,
        *,
        user_id: str,
        limit: int,
        offset: int,
    ) -> list[Conversation]:
        result = await self.session.scalars(
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def list_messages(
        self,
        conversation_id: str,
        *,
        user_id: str,
    ) -> list[Message]:
        result = await self.session.scalars(
            select(Message)
            .join(AgentRun, AgentRun.run_id == Message.run_id)
            .join(
                Conversation,
                Conversation.conversation_id == Message.conversation_id,
            )
            .where(
                Message.conversation_id == conversation_id,
                Conversation.user_id == user_id,
            )
            .order_by(
                AgentRun.turn_index,
                case((Message.role == "user", 0), else_=1),
                Message.message_id,
            )
        )
        return list(result)

    async def list_context_messages(
        self,
        conversation_id: str,
        *,
        user_id: str,
        exclude_run_id: str,
        limit: int,
    ) -> list[Message]:
        """读取当前轮之前最近完成的消息，并恢复为时间正序。"""
        result = await self.session.scalars(
            select(Message)
            .join(AgentRun, AgentRun.run_id == Message.run_id)
            .join(
                Conversation,
                Conversation.conversation_id == Message.conversation_id,
            )
            .where(
                Message.conversation_id == conversation_id,
                Conversation.user_id == user_id,
                Message.run_id != exclude_run_id,
                AgentRun.status == "completed",
            )
            .order_by(
                AgentRun.turn_index.desc(),
                case((Message.role == "assistant", 0), else_=1),
                Message.message_id.desc(),
            )
            .limit(limit)
        )
        return list(reversed(list(result)))


class FeedbackRepository:
    """验证运行存在后保存用户反馈。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        user_id: str,
        feedback_id: str,
        run_id: str,
        rating: int,
        comment: str | None,
        correction: str | None,
    ) -> UserFeedback | None:
        owned_run_id = await self.session.scalar(
            select(AgentRun.run_id)
            .join(
                Conversation,
                Conversation.conversation_id == AgentRun.conversation_id,
            )
            .where(AgentRun.run_id == run_id, Conversation.user_id == user_id)
        )
        if owned_run_id is None:
            return None
        feedback = UserFeedback(
            feedback_id=feedback_id,
            run_id=run_id,
            rating=rating,
            comment=comment,
            correction=correction,
        )
        self.session.add(feedback)
        await self.session.commit()
        return feedback
