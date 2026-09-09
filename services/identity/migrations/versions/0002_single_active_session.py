"""Keep only the newest login session for each account.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09
"""

from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sessions = sa.table(
        "auth_sessions",
        sa.column("session_id", sa.String(36)),
        sa.column("user_id", sa.String(36)),
        sa.column("revoked_at", sa.DateTime()),
        sa.column("created_at", sa.DateTime()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(sessions.c.session_id, sessions.c.user_id)
        .where(sessions.c.revoked_at.is_(None))
        .order_by(
            sessions.c.user_id,
            sessions.c.created_at.desc(),
            sessions.c.session_id.desc(),
        )
    ).all()

    retained_users: set[str] = set()
    revoked_session_ids: list[str] = []
    for session_id, user_id in rows:
        if user_id in retained_users:
            revoked_session_ids.append(session_id)
        else:
            retained_users.add(user_id)

    revoked_at = datetime.now(UTC).replace(tzinfo=None)
    for offset in range(0, len(revoked_session_ids), 500):
        batch = revoked_session_ids[offset : offset + 500]
        connection.execute(
            sa.update(sessions)
            .where(sessions.c.session_id.in_(batch))
            .values(revoked_at=revoked_at)
        )


def downgrade() -> None:
    raise RuntimeError("Revoked login sessions cannot be restored safely")
