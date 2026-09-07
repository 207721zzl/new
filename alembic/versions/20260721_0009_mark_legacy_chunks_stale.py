"""Mark pre-parent-child chunks as stale.

Revision ID: 20260721_0009
Revises: 20260721_0008
Create Date: 2026-07-21
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260721_0009"
down_revision: str | Sequence[str] | None = "20260721_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Prevent legacy child rows without a parent from appearing indexed."""
    op.execute(
        "UPDATE document_chunks "
        "SET index_status = 'stale' "
        "WHERE parent_chunk_id IS NULL"
    )


def downgrade() -> None:
    """Restore the legacy rows' previous completed-state convention."""
    op.execute(
        "UPDATE document_chunks "
        "SET index_status = 'completed' "
        "WHERE parent_chunk_id IS NULL AND index_status = 'stale'"
    )
