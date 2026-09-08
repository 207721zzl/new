"""Add indexes used by the administrator monitoring dashboards.

Revision ID: 20260908_0014
Revises: 20260906_0013
Create Date: 2026-09-08
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260908_0014"
down_revision: str | Sequence[str] | None = "20260906_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_agent_runs_created_at"),
        "agent_runs",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_feedback_created_at"),
        "user_feedback",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_feedback_created_at"), table_name="user_feedback")
    op.drop_index(op.f("ix_agent_runs_created_at"), table_name="agent_runs")
