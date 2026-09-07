"""Add parent chunks and link searchable child chunks.

Revision ID: 20260721_0008
Revises: 20260720_0007
Create Date: 2026-07-21
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260721_0008"
down_revision: str | Sequence[str] | None = "20260720_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the parent context store and link new child chunks to it."""
    op.create_table(
        "document_parent_chunks",
        sa.Column("parent_chunk_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("parent_index", sa.Integer(), nullable=False),
        sa.Column("doc_type", sa.String(length=64), nullable=False),
        sa.Column("metric_name", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=512), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(), nullable=False),
        sa.Column(
            "index_status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "parent_index >= 0",
            name=op.f("ck_document_parent_chunks_parent_index_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name=op.f("fk_document_parent_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "parent_chunk_id",
            name=op.f("pk_document_parent_chunks"),
        ),
    )
    op.create_index(
        op.f("ix_document_parent_chunks_document_id"),
        "document_parent_chunks",
        ["document_id"],
    )
    op.add_column(
        "document_chunks",
        sa.Column("parent_chunk_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        op.f("ix_document_chunks_parent_chunk_id"),
        "document_chunks",
        ["parent_chunk_id"],
    )
    op.create_foreign_key(
        op.f("fk_document_chunks_parent_chunk_id_document_parent_chunks"),
        "document_chunks",
        "document_parent_chunks",
        ["parent_chunk_id"],
        ["parent_chunk_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    """Remove parent-child chunk storage while preserving legacy child rows."""
    op.drop_constraint(
        op.f("fk_document_chunks_parent_chunk_id_document_parent_chunks"),
        "document_chunks",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_document_chunks_parent_chunk_id"),
        table_name="document_chunks",
    )
    op.drop_column("document_chunks", "parent_chunk_id")
    op.drop_index(
        op.f("ix_document_parent_chunks_document_id"),
        table_name="document_parent_chunks",
    )
    op.drop_table("document_parent_chunks")
