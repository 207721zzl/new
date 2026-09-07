"""EvidenceRAG 运行时以及旧迁移兼容所需的 MySQL ORM 模型。"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from packages.platform.tasks import TaskColumns
from services.knowledge.base import Base


MONEY_TYPE = Numeric(14, 2)


class IndexingJob(Base):
    """一次可重试、可审计的知识索引任务。"""

    __tablename__ = "indexing_jobs"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_by_user_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
    )
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    recreate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    document_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
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
        CheckConstraint("document_count >= 0", name="document_count_non_negative"),
        CheckConstraint("chunk_count >= 0", name="chunk_count_non_negative"),
        CheckConstraint(
            "file_size_bytes IS NULL OR file_size_bytes >= 0",
            name="file_size_bytes_non_negative",
        ),
    )
class DurableTask(TaskColumns, Base):
    __tablename__ = "durable_tasks"


class KnowledgeState(Base):
    __tablename__ = "knowledge_state"
    state_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collection: Mapped[str] = mapped_column(String(128))
    epoch: Mapped[int] = mapped_column(Integer, default=0)


class DocumentHead(Base):
    __tablename__ = "document_heads"
    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    active_snapshot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class DocumentSnapshot(Base):
    __tablename__ = "document_snapshots"
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128), index=True)
    collection: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class KnowledgeAudit(Base):
    __tablename__ = "knowledge_audits"
    audit_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_user_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(128))
    document_id: Mapped[str] = mapped_column(String(128))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)
