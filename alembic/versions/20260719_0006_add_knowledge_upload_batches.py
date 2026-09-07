"""Add batch upload metadata to knowledge indexing jobs.

Revision ID: 20260719_0006
Revises: 20260718_0005
Create Date: 2026-07-19
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260719_0006"
down_revision: str | Sequence[str] | None = "20260718_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist the upload batch and original file metadata on each index job."""
    op.add_column(
        "indexing_jobs",
        sa.Column("batch_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "indexing_jobs",
        sa.Column("original_filename", sa.String(255), nullable=True),
    )
    op.add_column(
        "indexing_jobs",
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "indexing_jobs",
        sa.Column("content_sha256", sa.String(64), nullable=True),
    )
    op.create_index(
        op.f("ix_indexing_jobs_batch_id"),
        "indexing_jobs",
        ["batch_id"],
    )
    op.create_check_constraint(
        op.f("ck_indexing_jobs_file_size_bytes_non_negative"),
        "indexing_jobs",
        "file_size_bytes IS NULL OR file_size_bytes >= 0",
    )


def downgrade() -> None:
    """Remove upload batch metadata."""
    op.drop_constraint(
        op.f("ck_indexing_jobs_file_size_bytes_non_negative"),
        "indexing_jobs",
        type_="check",
    )
    op.drop_index(op.f("ix_indexing_jobs_batch_id"), table_name="indexing_jobs")
    op.drop_column("indexing_jobs", "content_sha256")
    op.drop_column("indexing_jobs", "file_size_bytes")
    op.drop_column("indexing_jobs", "original_filename")
    op.drop_column("indexing_jobs", "batch_id")
