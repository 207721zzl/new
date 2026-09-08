"""Add time indexes for administrator monitoring queries."""

from alembic import op


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
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


def downgrade():
    op.drop_index(op.f("ix_user_feedback_created_at"), table_name="user_feedback")
    op.drop_index(op.f("ix_agent_runs_created_at"), table_name="agent_runs")
