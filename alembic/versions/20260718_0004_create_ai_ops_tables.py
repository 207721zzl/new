"""Create knowledge indexing, conversation, tool and evaluation tables.

Revision ID: 20260718_0004
Revises: 20260718_0003
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260718_0004"
down_revision: str | Sequence[str] | None = "20260718_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the AI system tables described by the PRD."""
    op.create_table(
        "documents",
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("doc_type", sa.String(64), nullable=False),
        sa.Column("metric_name", sa.String(128), nullable=False),
        sa.Column("source", sa.String(512), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(), nullable=False),
        sa.Column("index_status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("document_id", name=op.f("pk_documents")),
    )
    op.create_index(op.f("ix_documents_doc_type"), "documents", ["doc_type"])
    op.create_index(op.f("ix_documents_metric_name"), "documents", ["metric_name"])

    op.create_table(
        "document_chunks",
        sa.Column("chunk_id", sa.String(64), nullable=False),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("doc_type", sa.String(64), nullable=False),
        sa.Column("metric_name", sa.String(128), nullable=False),
        sa.Column("source", sa.String(512), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(), nullable=False),
        sa.Column("index_status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("chunk_index >= 0", name=op.f("ck_document_chunks_chunk_index_non_negative")),
        sa.ForeignKeyConstraint(["document_id"], ["documents.document_id"], name=op.f("fk_document_chunks_document_id_documents"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chunk_id", name=op.f("pk_document_chunks")),
    )
    op.create_index(op.f("ix_document_chunks_document_id"), "document_chunks", ["document_id"])

    op.create_table(
        "indexing_jobs",
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("source_path", sa.String(1024), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("recreate", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("document_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("document_count >= 0", name=op.f("ck_indexing_jobs_document_count_non_negative")),
        sa.CheckConstraint("chunk_count >= 0", name=op.f("ck_indexing_jobs_chunk_count_non_negative")),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_indexing_jobs")),
    )
    op.create_index(op.f("ix_indexing_jobs_status"), "indexing_jobs", ["status"])

    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], name=op.f("fk_conversations_run_id_agent_runs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("conversation_id", name=op.f("pk_conversations")),
        sa.UniqueConstraint("run_id", name=op.f("uq_conversations_run_id")),
    )

    op.create_table(
        "messages",
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.conversation_id"], name=op.f("fk_messages_conversation_id_conversations"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], name=op.f("fk_messages_run_id_agent_runs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("message_id", name=op.f("pk_messages")),
    )
    op.create_index(op.f("ix_messages_conversation_id"), "messages", ["conversation_id"])
    op.create_index(op.f("ix_messages_run_id"), "messages", ["run_id"])
    op.create_index(op.f("ix_messages_created_at"), "messages", ["created_at"])

    op.create_table(
        "tool_calls",
        sa.Column("tool_call_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("output_payload", sa.JSON(), nullable=True),
        sa.Column("duration_ms", sa.Numeric(14, 3), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name=op.f("ck_tool_calls_duration_non_negative")),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], name=op.f("fk_tool_calls_run_id_agent_runs"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("tool_call_id", name=op.f("pk_tool_calls")),
    )
    op.create_index(op.f("ix_tool_calls_run_id"), "tool_calls", ["run_id"])
    op.create_index(op.f("ix_tool_calls_tool_name"), "tool_calls", ["tool_name"])
    op.create_index(op.f("ix_tool_calls_created_at"), "tool_calls", ["created_at"])

    op.create_table(
        "user_feedback",
        sa.Column("feedback_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("correction", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("rating IN (-1, 1)", name=op.f("ck_user_feedback_rating_allowed")),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], name=op.f("fk_user_feedback_run_id_agent_runs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("feedback_id", name=op.f("pk_user_feedback")),
    )
    op.create_index(op.f("ix_user_feedback_run_id"), "user_feedback", ["run_id"])

    op.create_table(
        "eval_cases",
        sa.Column("case_id", sa.String(128), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expected", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("case_id", name=op.f("pk_eval_cases")),
    )
    op.create_index(op.f("ix_eval_cases_category"), "eval_cases", ["category"])


def downgrade() -> None:
    """Drop the AI system tables in reverse dependency order."""
    op.drop_table("eval_cases")
    op.drop_table("user_feedback")
    op.drop_table("tool_calls")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("indexing_jobs")
    op.drop_table("document_chunks")
    op.drop_table("documents")
