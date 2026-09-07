"""Generalize knowledge metadata for a standalone multi-format RAG system.

Revision ID: 20260812_0010
Revises: 20260721_0009
Create Date: 2026-08-12
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260812_0010"
down_revision: str | Sequence[str] | None = "20260721_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("original_filename", sa.String(255)))
    op.add_column("documents", sa.Column("mime_type", sa.String(128)))
    op.add_column("documents", sa.Column("content_sha256", sa.String(64)))
    op.add_column(
        "documents",
        sa.Column("language", sa.String(16), server_default="zh-CN", nullable=False),
    )
    op.add_column(
        "documents",
        sa.Column(
            "metadata",
            sa.JSON(),
            server_default=sa.text("(JSON_OBJECT())"),
            nullable=False,
        ),
    )

    for table in ("document_parent_chunks", "document_chunks"):
        op.add_column(
            table,
            sa.Column(
                "section_path",
                sa.JSON(),
                server_default=sa.text("(JSON_ARRAY())"),
                nullable=False,
            ),
        )
        op.add_column(table, sa.Column("page_start", sa.Integer()))
        op.add_column(table, sa.Column("page_end", sa.Integer()))
        op.add_column(
            table,
            sa.Column("block_type", sa.String(64), server_default="mixed", nullable=False),
        )
        op.add_column(
            table,
            sa.Column("language", sa.String(16), server_default="zh-CN", nullable=False),
        )
    op.add_column(
        "document_parent_chunks",
        sa.Column(
            "metadata",
            sa.JSON(),
            server_default=sa.text("(JSON_OBJECT())"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("document_parent_chunks", "metadata")
    for table in ("document_chunks", "document_parent_chunks"):
        op.drop_column(table, "language")
        op.drop_column(table, "block_type")
        op.drop_column(table, "page_end")
        op.drop_column(table, "page_start")
        op.drop_column(table, "section_path")
    op.drop_column("documents", "metadata")
    op.drop_column("documents", "language")
    op.drop_column("documents", "content_sha256")
    op.drop_column("documents", "mime_type")
    op.drop_column("documents", "original_filename")
