"""角色权限、临时密码门禁与资源归属查询测试。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.auth.constants import ROLE_ADMIN, ROLE_EMPLOYEE, USER_STATUS_ACTIVE
from app.auth.dependencies import require_admin, require_employee
from app.auth.service import AuthContext
from app.db.models import AuthSession, User
from app.db.repositories import AgentRunRepository, ConversationRepository
from app.errors import PasswordChangeRequiredError, PermissionDeniedError


def _context(*, role: str, must_change_password: bool = False) -> AuthContext:
    now = datetime.now(UTC).replace(tzinfo=None)
    user = User(
        user_id="user-1",
        username="person.one",
        normalized_username="person.one",
        display_name="测试用户",
        password_hash="not-used",
        role=role,
        status=USER_STATUS_ACTIVE,
        must_change_password=must_change_password,
        failed_login_count=0,
    )
    auth_session = AuthSession(
        session_id="session-1",
        user_id=user.user_id,
        token_hash="token-hash",
        csrf_token_hash="csrf-hash",
        expires_at=now + timedelta(hours=1),
        last_seen_at=now,
        created_at=now,
    )
    return AuthContext(user=user, auth_session=auth_session)


def test_employee_can_only_use_query_routes() -> None:
    context = _context(role=ROLE_EMPLOYEE)

    assert asyncio.run(require_employee(context)) is context
    with pytest.raises(PermissionDeniedError):
        asyncio.run(require_admin(context))


def test_admin_can_only_use_management_routes() -> None:
    context = _context(role=ROLE_ADMIN)

    assert asyncio.run(require_admin(context)) is context
    with pytest.raises(PermissionDeniedError):
        asyncio.run(require_employee(context))


def test_temporary_password_blocks_both_role_features() -> None:
    context = _context(role=ROLE_ADMIN, must_change_password=True)

    with pytest.raises(PasswordChangeRequiredError):
        asyncio.run(require_employee(context))
    with pytest.raises(PasswordChangeRequiredError):
        asyncio.run(require_admin(context))


class CaptureSession:
    def __init__(self) -> None:
        self.statement = None

    async def scalar(self, statement):
        self.statement = statement
        return None

    async def scalars(self, statement):
        self.statement = statement
        return []


def test_run_and_conversation_queries_include_user_ownership_predicate() -> None:
    run_session = CaptureSession()
    conversation_session = CaptureSession()

    asyncio.run(AgentRunRepository(run_session).get_for_user("run-1", "owner-1"))
    asyncio.run(
        ConversationRepository(conversation_session).list_recent(
            user_id="owner-1",
            limit=20,
            offset=0,
        )
    )

    run_sql = str(run_session.statement)
    conversation_sql = str(conversation_session.statement)
    assert "conversations.user_id" in run_sql
    assert "conversations.user_id" in conversation_sql
