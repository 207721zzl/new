"""EvidenceRAG 运行时以及旧迁移兼容所需的 MySQL ORM 模型。"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from packages.platform.tasks import TaskColumns
from services.chat.base import Base


MONEY_TYPE = Numeric(14, 2)


class AgentRun(Base):
    """一次可恢复、可审计的 RAG 后台运行。"""

    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    top_k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="queued",
        server_default="queued",
        index=True,
    )
    current_node: Mapped[str] = mapped_column(
        String(64), nullable=False, default="queued", server_default="queued"
    )
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp(), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint("turn_index > 0", name="turn_index_positive"),
        UniqueConstraint(
            "conversation_id",
            "turn_index",
            name="uq_agent_runs_conversation_turn",
        ),
    )


class Conversation(Base):
    """页面和 API 可浏览的一次 RAG 会话。"""

    __tablename__ = "conversations"

    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class Message(Base):
    """会话中的用户问题或 RAG 最终回答。"""

    __tablename__ = "messages"

    message_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp(), index=True
    )


    __table_args__ = (UniqueConstraint("run_id", "role", name="uq_messages_run_role"),)


class UserFeedback(Base):
    """用户对一次运行提交的点赞、点踩、备注或纠错。"""

    __tablename__ = "user_feedback"

    feedback_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp(), index=True
    )

    __table_args__ = (CheckConstraint("rating IN (-1, 1)", name="rating_allowed"),)
class DurableTask(TaskColumns, Base):
    __tablename__ = "durable_tasks"


class MaintenanceReceipt(Base):
    __tablename__ = "maintenance_receipts"
    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    result: Mapped[dict] = mapped_column(JSON)
