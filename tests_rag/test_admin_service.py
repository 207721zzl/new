"""管理员服务关键保护规则测试。"""

import asyncio
from datetime import UTC, datetime

import pytest

from app.admin.service import AdminService
from app.auth.constants import ROLE_ADMIN, ROLE_EMPLOYEE, USER_STATUS_ACTIVE
from app.auth.security import verify_password
from app.db.models import AdminAuditLog, User
from app.errors import ProtectedAdministratorError


def _user(*, user_id: str, role: str = ROLE_ADMIN) -> User:
    now = datetime.now(UTC).replace(tzinfo=None)
    return User(
        user_id=user_id,
        username=user_id,
        normalized_username=user_id,
        display_name=user_id,
        password_hash="old-password-hash",
        role=role,
        status=USER_STATUS_ACTIVE,
        must_change_password=False,
        failed_login_count=3,
        created_at=now,
        updated_at=now,
    )


class FakeSession:
    def __init__(self, scalar_results=()) -> None:
        self.scalar_results = list(scalar_results)
        self.added = []
        self.executed = []
        self.commits = 0

    async def scalar(self, _statement):
        return self.scalar_results.pop(0)

    async def execute(self, statement):
        self.executed.append(statement)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def test_admin_cannot_disable_or_demote_own_account() -> None:
    admin = _user(user_id="admin-1")
    session = FakeSession([admin])

    with pytest.raises(ProtectedAdministratorError):
        asyncio.run(
            AdminService(session).update_user(
                actor_user_id="admin-1",
                user_id="admin-1",
                display_name=None,
                role=ROLE_EMPLOYEE,
                status=None,
                request_id="request-1",
                ip_address="127.0.0.1",
            )
        )

    assert session.commits == 0


def test_last_active_administrator_is_protected() -> None:
    admin = _user(user_id="admin-2")
    session = FakeSession([admin, 1])

    with pytest.raises(ProtectedAdministratorError):
        asyncio.run(
            AdminService(session).update_user(
                actor_user_id="admin-1",
                user_id="admin-2",
                display_name=None,
                role=ROLE_EMPLOYEE,
                status=None,
                request_id="request-1",
                ip_address="127.0.0.1",
            )
        )

    assert session.commits == 0


def test_password_reset_revokes_sessions_and_forces_user_change() -> None:
    employee = _user(user_id="employee-1", role=ROLE_EMPLOYEE)
    session = FakeSession([employee])

    result = asyncio.run(
        AdminService(session).reset_password(
            actor_user_id="admin-1",
            user_id="employee-1",
            temporary_password="new temporary password",
            request_id="request-1",
            ip_address="127.0.0.1",
        )
    )

    assert result.must_change_password is True
    assert result.failed_login_count == 0
    assert verify_password("new temporary password", result.password_hash)
    assert len(session.executed) == 1
    assert isinstance(session.added[0], AdminAuditLog)
    assert session.commits == 1

