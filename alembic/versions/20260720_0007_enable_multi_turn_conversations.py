"""Allow one conversation to contain multiple agent runs.

Revision ID: 20260720_0007
Revises: 20260719_0006
Create Date: 2026-07-20
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0007"
down_revision: str | Sequence[str] | None = "20260719_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Move conversation ownership from a single run to all runs in the thread."""
    # Repair any legacy run that was committed before its conversation record.
    op.execute(
        sa.text(
            """
            INSERT INTO conversations (
                conversation_id, title, run_id, created_at, updated_at
            )
            SELECT
                agent_runs.run_id,
                LEFT(agent_runs.question, 256),
                agent_runs.run_id,
                agent_runs.created_at,
                agent_runs.updated_at
            FROM agent_runs
            LEFT JOIN conversations
              ON conversations.run_id = agent_runs.run_id
            WHERE conversations.conversation_id IS NULL
            """
        )
    )

    op.add_column(
        "agent_runs",
        sa.Column("conversation_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "agent_runs",
        sa.Column("turn_index", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE agent_runs
            JOIN conversations
              ON conversations.run_id = agent_runs.run_id
            SET agent_runs.conversation_id = conversations.conversation_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE agent_runs
            JOIN (
                SELECT *
                FROM (
                    SELECT
                        run_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY conversation_id
                            ORDER BY created_at, run_id
                        ) AS resolved_turn_index
                    FROM agent_runs
                ) AS ranked_source
            ) AS ranked_runs
              ON ranked_runs.run_id = agent_runs.run_id
            SET agent_runs.turn_index = ranked_runs.resolved_turn_index
            """
        )
    )

    op.drop_constraint(
        op.f("fk_conversations_run_id_agent_runs"),
        "conversations",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("uq_conversations_run_id"),
        "conversations",
        type_="unique",
    )
    op.drop_column("conversations", "run_id")

    op.alter_column(
        "agent_runs",
        "conversation_id",
        existing_type=sa.String(36),
        nullable=False,
    )
    op.alter_column(
        "agent_runs",
        "turn_index",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.create_index(
        op.f("ix_agent_runs_conversation_id"),
        "agent_runs",
        ["conversation_id"],
    )
    op.create_foreign_key(
        op.f("fk_agent_runs_conversation_id_conversations"),
        "agent_runs",
        "conversations",
        ["conversation_id"],
        ["conversation_id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        op.f("ck_agent_runs_turn_index_positive"),
        "agent_runs",
        "turn_index > 0",
    )
    op.create_unique_constraint(
        "uq_agent_runs_conversation_turn",
        "agent_runs",
        ["conversation_id", "turn_index"],
    )


def downgrade() -> None:
    """Restore the legacy one-run-per-conversation schema."""
    op.drop_constraint(
        "uq_agent_runs_conversation_turn",
        "agent_runs",
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_agent_runs_turn_index_positive"),
        "agent_runs",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_agent_runs_conversation_id_conversations"),
        "agent_runs",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_agent_runs_conversation_id"), table_name="agent_runs")

    op.add_column(
        "conversations",
        sa.Column("run_id", sa.String(36), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE conversations
            SET conversations.run_id = (
                SELECT agent_runs.run_id
                FROM agent_runs
                WHERE agent_runs.conversation_id = conversations.conversation_id
                ORDER BY agent_runs.created_at, agent_runs.run_id
                LIMIT 1
            )
            """
        )
    )
    op.alter_column(
        "conversations",
        "run_id",
        existing_type=sa.String(36),
        nullable=False,
    )
    op.create_unique_constraint(
        op.f("uq_conversations_run_id"),
        "conversations",
        ["run_id"],
    )
    op.create_foreign_key(
        op.f("fk_conversations_run_id_agent_runs"),
        "conversations",
        "agent_runs",
        ["run_id"],
        ["run_id"],
        ondelete="CASCADE",
    )
    op.drop_column("agent_runs", "conversation_id")
    op.drop_column("agent_runs", "turn_index")
