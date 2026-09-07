"""Require explicit authenticated owners for new business records.

Revision ID: 20260906_0013
Revises: 20260904_0012
Create Date: 2026-09-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260906_0013"
down_revision: str | Sequence[str] | None = "20260904_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LEGACY_USER_ID = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    """Remove legacy defaults so every new record must name its real owner."""
    op.alter_column(
        "conversations",
        "user_id",
        existing_type=sa.String(36),
        existing_nullable=False,
        server_default=None,
    )
    op.alter_column(
        "indexing_jobs",
        "created_by_user_id",
        existing_type=sa.String(36),
        existing_nullable=False,
        server_default=None,
    )


def downgrade() -> None:
    """Restore only the compatibility defaults; existing ownership is retained."""
    op.alter_column(
        "indexing_jobs",
        "created_by_user_id",
        existing_type=sa.String(36),
        existing_nullable=False,
        server_default=LEGACY_USER_ID,
    )
    op.alter_column(
        "conversations",
        "user_id",
        existing_type=sa.String(36),
        existing_nullable=False,
        server_default=LEGACY_USER_ID,
    )
