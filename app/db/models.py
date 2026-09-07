"""EvidenceRAG 运行时以及旧迁移兼容所需的 MySQL ORM 模型。"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


MONEY_TYPE = Numeric(14, 2)


class Customer(Base):
    """用于客户分层和区域分析的匿名化客户维表。"""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    customer_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    region: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    customer_level: Mapped[str] = mapped_column(String(32), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class Campaign(Base):
    """营销活动及预算、渠道和生效周期。"""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    campaign_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    budget: Mapped[Decimal] = mapped_column(MONEY_TYPE, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        CheckConstraint("budget >= 0", name="budget_non_negative"),
        CheckConstraint("end_at > start_at", name="campaign_period_valid"),
    )


class Product(Base):
    """可售商品及其标准分类和单价。"""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    unit_price: Mapped[Decimal] = mapped_column(MONEY_TYPE, nullable=False)
    stock_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("stock_quantity >= 0", name="stock_quantity_non_negative"),
    )


class Order(Base):
    """订单主表；区域和下单时间是经营分析的常用维度。"""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    order_status: Mapped[str] = mapped_column(String(32), nullable=False)
    order_amount: Mapped[Decimal] = mapped_column(MONEY_TYPE, nullable=False)
    ordered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        CheckConstraint("order_amount >= 0", name="order_amount_non_negative"),
        Index("ix_orders_ordered_at_region", "ordered_at", "region"),
    )


class DailyTraffic(Base):
    """按日期、区域和渠道聚合的流量转化漏斗事实。"""

    __tablename__ = "daily_traffic"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    traffic_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    visits: Mapped[int] = mapped_column(Integer, nullable=False)
    product_views: Mapped[int] = mapped_column(Integer, nullable=False)
    add_to_cart_users: Mapped[int] = mapped_column(Integer, nullable=False)
    checkout_users: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_users: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        CheckConstraint("visits >= 0", name="visits_non_negative"),
        CheckConstraint("product_views >= 0", name="product_views_non_negative"),
        CheckConstraint("add_to_cart_users >= 0", name="cart_users_non_negative"),
        CheckConstraint("checkout_users >= 0", name="checkout_users_non_negative"),
        CheckConstraint("paid_users >= 0", name="paid_users_non_negative"),
        CheckConstraint("product_views >= visits", name="views_cover_visits"),
        CheckConstraint(
            "visits >= add_to_cart_users",
            name="visits_cover_cart_users",
        ),
        CheckConstraint(
            "add_to_cart_users >= checkout_users",
            name="cart_users_cover_checkout_users",
        ),
        CheckConstraint(
            "checkout_users >= paid_users",
            name="checkout_users_cover_paid_users",
        ),
    )


class OrderItem(Base):
    """订单商品明细，是品类和商品维度分析的事实表。"""

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY_TYPE, nullable=False)
    line_amount: Mapped[Decimal] = mapped_column(MONEY_TYPE, nullable=False)

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("line_amount >= 0", name="line_amount_non_negative"),
    )


class Payment(Base):
    """支付事实；GMV 以成功支付金额为准。"""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    paid_amount: Mapped[Decimal] = mapped_column(MONEY_TYPE, nullable=False)
    payment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    __table_args__ = (
        CheckConstraint("paid_amount >= 0", name="paid_amount_non_negative"),
    )


class Refund(Base):
    """退款事实；仅成功退款会进入退款率口径。"""

    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    refund_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_items.id", ondelete="SET NULL"), nullable=True
    )
    refund_amount: Mapped[Decimal] = mapped_column(MONEY_TYPE, nullable=False)
    refund_status: Mapped[str] = mapped_column(String(32), nullable=False)
    refunded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    __table_args__ = (
        CheckConstraint("refund_amount >= 0", name="refund_amount_non_negative"),
    )


class MetricDefinition(Base):
    """指标语义层：统一业务名称、公式、来源表和版本。"""

    __tablename__ = "metric_definitions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    metric_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    formula: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    time_grain: Mapped[str] = mapped_column(String(32), nullable=False)
    source_tables: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    def as_dict(self) -> dict[str, Any]:
        """返回可直接用于 Prompt 或 API 的指标元数据。"""
        return {
            "metric_code": self.metric_code,
            "metric_name": self.metric_name,
            "description": self.description,
            "formula": self.formula,
            "unit": self.unit,
            "time_grain": self.time_grain,
            "source_tables": self.source_tables,
            "version": self.version,
        }


class User(Base):
    """可以登录系统并拥有独立会话空间的用户。"""

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_username: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, default="employee", server_default="employee"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint("role IN ('admin', 'employee')", name="role_allowed"),
        CheckConstraint(
            "status IN ('pending', 'active', 'disabled')",
            name="status_allowed",
        ),
        CheckConstraint(
            "failed_login_count >= 0", name="failed_login_count_non_negative"
        ),
        UniqueConstraint(
            "normalized_username", name="uq_users_normalized_username"
        ),
        Index("ix_users_role_status", "role", "status"),
    )


class AuthSession(Base):
    """服务端保存的可撤销登录会话；浏览器只持有原始随机令牌。"""

    __tablename__ = "auth_sessions"

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        Index("ix_auth_sessions_user_revoked", "user_id", "revoked_at"),
    )


class AdminAuditLog(Base):
    """管理员与系统执行敏感操作时留下的不可变审计事件。"""

    __tablename__ = "admin_audit_logs"

    audit_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp(), index=True
    )

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('success', 'failure', 'denied')",
            name="outcome_allowed",
        ),
    )


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
        CheckConstraint("turn_index > 0", name="turn_index_positive"),
        UniqueConstraint(
            "conversation_id",
            "turn_index",
            name="uq_agent_runs_conversation_turn",
        ),
    )


class KnowledgeDocumentRecord(Base):
    """MySQL 中的知识原文与索引状态真源。"""

    __tablename__ = "documents"

    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str] = mapped_column(
        Text().with_variant(mysql.LONGTEXT(), "mysql"),
        nullable=False,
    )
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="zh-CN", server_default="zh-CN"
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    index_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class KnowledgeParentChunkRecord(Base):
    """用于生成阶段恢复完整上下文的父知识块。"""

    __tablename__ = "document_parent_chunks"

    parent_chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    parent_index: Mapped[int] = mapped_column(Integer, nullable=False)
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    section_path: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    block_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="mixed", server_default="mixed"
    )
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="zh-CN", server_default="zh-CN"
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    index_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint("parent_index >= 0", name="parent_index_non_negative"),
    )


class KnowledgeChunkRecord(Base):
    """与 Milvus chunk_id 一一对齐的可检索子块元数据。"""

    __tablename__ = "document_chunks"

    chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_chunk_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "document_parent_chunks.parent_chunk_id",
            ondelete="CASCADE",
        ),
        nullable=True,
        index=True,
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    section_path: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    block_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="mixed", server_default="mixed"
    )
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="zh-CN", server_default="zh-CN"
    )
    index_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
    )


class IndexingJob(Base):
    """一次可重试、可审计的知识索引任务。"""

    __tablename__ = "indexing_jobs"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="RESTRICT"),
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


class Conversation(Base):
    """页面和 API 可浏览的一次 RAG 会话。"""

    __tablename__ = "conversations"

    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="RESTRICT"),
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


class ToolCall(Base):
    """MCP 工具调用、耗时、输入输出与错误审计记录。"""

    __tablename__ = "tool_calls"

    tool_call_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp(), index=True
    )

    __table_args__ = (
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0", name="duration_non_negative"
        ),
    )


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
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (CheckConstraint("rating IN (-1, 1)", name="rating_allowed"),)


class EvaluationCase(Base):
    """可版本化维护的离线评测用例目录。"""

    __tablename__ = "eval_cases"

    case_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    expected: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
