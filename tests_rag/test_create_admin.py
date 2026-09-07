"""首个管理员创建脚本的事务和保护规则测试。"""

import asyncio

import pytest

from app.auth.constants import ROLE_ADMIN, USER_STATUS_ACTIVE
from app.auth.security import verify_password
from app.db.models import AdminAuditLog, User
from scripts.create_admin import BootstrapAdminExistsError, create_initial_admin


class FakeSession:
    def __init__(self, scalar_results) -> None:
        self.scalar_results = list(scalar_results)
        self.added = []
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    async def scalar(self, _statement):
        return self.scalar_results.pop(0)

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


def test_create_initial_admin_hashes_password_and_writes_audit_event() -> None:
    session = FakeSession([object(), None, None])

    user = asyncio.run(
        create_initial_admin(
            session,
            username="Root.Admin",
            display_name="系统管理员",
            password="correct horse battery staple",
        )
    )

    assert user.username == "root.admin"
    assert user.role == ROLE_ADMIN
    assert user.status == USER_STATUS_ACTIVE
    assert verify_password("correct horse battery staple", user.password_hash)
    assert isinstance(session.added[0], User)
    assert isinstance(session.added[1], AdminAuditLog)
    assert session.added[1].actor_user_id == user.user_id
    assert session.flush_count == 1
    assert session.commit_count == 1
    assert session.rollback_count == 0


def test_bootstrap_refuses_to_create_a_second_admin() -> None:
    session = FakeSession([object(), "existing-admin-id"])

    with pytest.raises(BootstrapAdminExistsError):
        asyncio.run(
            create_initial_admin(
                session,
                username="another.admin",
                display_name="Another admin",
                password="correct horse battery staple",
            )
        )

    assert session.added == []
    assert session.commit_count == 0
    assert session.rollback_count == 1
