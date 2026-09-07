"""Add users, server-side sessions, audit logs and resource ownership.

Revision ID: 20260904_0012
Revises: 20260812_0011
Create Date: 2026-09-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260904_0012"
down_revision: str | Sequence[str] | None = "20260812_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LEGACY_USER_ID = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    """Create the authentication foundation without breaking the legacy UI."""
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("normalized_username", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "role", sa.String(32), server_default="employee", nullable=False
        ),
        sa.Column(
            "status", sa.String(32), server_default="pending", nullable=False
        ),
        sa.Column(
            "must_change_password", sa.Boolean(), server_default="0", nullable=False
        ),
        sa.Column(
            "failed_login_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column(
            "password_changed_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.String(36), nullable=True),
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
            "role IN ('admin', 'employee')",
            name=op.f("ck_users_role_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'disabled')",
            name=op.f("ck_users_status_allowed"),
        ),
        sa.CheckConstraint(
            "failed_login_count >= 0",
            name=op.f("ck_users_failed_login_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.user_id"],
            name=op.f("fk_users_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_users")),
        sa.UniqueConstraint(
            "normalized_username",
            name="uq_users_normalized_username",
        ),
    )
    op.create_index(
        op.f("ix_users_created_by_user_id"),
        "users",
        ["created_by_user_id"],
    )
    op.create_index("ix_users_role_status", "users", ["role", "status"])

    # 历史数据拥有一个不可登录的固定主体，避免迁移后出现无归属会话。
    users = sa.table(
        "users",
        sa.column("user_id", sa.String(36)),
        sa.column("username", sa.String(64)),
        sa.column("normalized_username", sa.String(64)),
        sa.column("display_name", sa.String(128)),
        sa.column("password_hash", sa.String(255)),
        sa.column("role", sa.String(32)),
        sa.column("status", sa.String(32)),
        sa.column("must_change_password", sa.Boolean()),
        sa.column("failed_login_count", sa.Integer()),
    )
    op.bulk_insert(
        users,
        [
            {
                "user_id": LEGACY_USER_ID,
                "username": "__legacy_system__",
                "normalized_username": "__legacy_system__",
                "display_name": "Legacy data owner",
                "password_hash": "!disabled-system-account",
                "role": "employee",
                "status": "disabled",
                "must_change_password": False,
                "failed_login_count": 0,
            }
        ],
    )

    op.create_table(
        "auth_sessions",
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_auth_sessions_expiry_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_auth_sessions")),
        sa.UniqueConstraint(
            "token_hash", name=op.f("uq_auth_sessions_token_hash")
        ),
    )
    op.create_index(
        op.f("ix_auth_sessions_user_id"), "auth_sessions", ["user_id"]
    )
    op.create_index(
        op.f("ix_auth_sessions_expires_at"), "auth_sessions", ["expires_at"]
    )
    op.create_index(
        "ix_auth_sessions_user_revoked",
        "auth_sessions",
        ["user_id", "revoked_at"],
    )

    op.create_table(
        "admin_audit_logs",
        sa.Column("audit_id", sa.String(36), nullable=False),
        sa.Column("actor_user_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column(
            "details",
            sa.JSON(),
            server_default=sa.text("(JSON_OBJECT())"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'failure', 'denied')",
            name=op.f("ck_admin_audit_logs_outcome_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.user_id"],
            name=op.f("fk_admin_audit_logs_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("audit_id", name=op.f("pk_admin_audit_logs")),
    )
    op.create_index(
        op.f("ix_admin_audit_logs_actor_user_id"),
        "admin_audit_logs",
        ["actor_user_id"],
    )
    op.create_index(
        op.f("ix_admin_audit_logs_action"), "admin_audit_logs", ["action"]
    )
    op.create_index(
        op.f("ix_admin_audit_logs_created_at"),
        "admin_audit_logs",
        ["created_at"],
    )

    op.add_column(
        "conversations",
        sa.Column(
            "user_id",
            sa.String(36),
            server_default=LEGACY_USER_ID,
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE conversations SET user_id = :legacy_user_id "
            "WHERE user_id IS NULL"
        ).bindparams(legacy_user_id=LEGACY_USER_ID)
    )
    op.alter_column(
        "conversations",
        "user_id",
        existing_type=sa.String(36),
        server_default=LEGACY_USER_ID,
        nullable=False,
    )
    op.create_index(
        op.f("ix_conversations_user_id"), "conversations", ["user_id"]
    )
    op.create_foreign_key(
        op.f("fk_conversations_user_id_users"),
        "conversations",
        "users",
        ["user_id"],
        ["user_id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "indexing_jobs",
        sa.Column(
            "created_by_user_id",
            sa.String(36),
            server_default=LEGACY_USER_ID,
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE indexing_jobs SET created_by_user_id = :legacy_user_id "
            "WHERE created_by_user_id IS NULL"
        ).bindparams(legacy_user_id=LEGACY_USER_ID)
    )
    op.alter_column(
        "indexing_jobs",
        "created_by_user_id",
        existing_type=sa.String(36),
        server_default=LEGACY_USER_ID,
        nullable=False,
    )
    op.create_index(
        op.f("ix_indexing_jobs_created_by_user_id"),
        "indexing_jobs",
        ["created_by_user_id"],
    )
    op.create_foreign_key(
        op.f("fk_indexing_jobs_created_by_user_id_users"),
        "indexing_jobs",
        "users",
        ["created_by_user_id"],
        ["user_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Remove authentication tables and ownership columns."""
    op.drop_constraint(
        op.f("fk_indexing_jobs_created_by_user_id_users"),
        "indexing_jobs",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_indexing_jobs_created_by_user_id"),
        table_name="indexing_jobs",
    )
    op.drop_column("indexing_jobs", "created_by_user_id")

    op.drop_constraint(
        op.f("fk_conversations_user_id_users"),
        "conversations",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_conversations_user_id"), table_name="conversations"
    )
    op.drop_column("conversations", "user_id")

    op.drop_table("admin_audit_logs")
    op.drop_table("auth_sessions")
    op.drop_table("users")
