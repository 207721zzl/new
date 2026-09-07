"""通过交互式密码输入安全创建系统的首个管理员。"""

import argparse
import asyncio
from collections.abc import Sequence
from getpass import getpass
import sys
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.constants import (
    AUDIT_OUTCOME_SUCCESS,
    LEGACY_USER_ID,
    ROLE_ADMIN,
    USER_STATUS_ACTIVE,
)
from app.auth.security import (
    hash_password,
    validate_display_name,
    validate_username,
)
from app.db.models import AdminAuditLog, User
from app.db.session import get_admin_session_factory


class BootstrapAdminExistsError(RuntimeError):
    """系统中已经存在管理员，禁止重复使用引导入口。"""


async def create_initial_admin(
    session: AsyncSession,
    *,
    username: str,
    display_name: str,
    password: str,
    must_change_password: bool = False,
) -> User:
    """在锁定系统归属账号后，原子创建唯一的首个管理员。"""
    normalized_username = validate_username(username)
    normalized_display_name = validate_display_name(display_name)
    password_hash = hash_password(password)

    try:
        # 锁定迁移创建的固定行，避免两个引导进程同时创建“首个”管理员。
        legacy_owner = await session.scalar(
            select(User)
            .where(User.user_id == LEGACY_USER_ID)
            .with_for_update()
        )
        if legacy_owner is None:
            raise RuntimeError(
                "authentication tables are not initialized; run alembic upgrade head"
            )

        existing_admin_id = await session.scalar(
            select(User.user_id).where(User.role == ROLE_ADMIN).limit(1)
        )
        if existing_admin_id is not None:
            raise BootstrapAdminExistsError(
                "an administrator already exists; use account management instead"
            )

        existing_username_id = await session.scalar(
            select(User.user_id)
            .where(User.normalized_username == normalized_username)
            .limit(1)
        )
        if existing_username_id is not None:
            raise ValueError("username is already in use")

        user_id = str(uuid4())
        user = User(
            user_id=user_id,
            username=normalized_username,
            normalized_username=normalized_username,
            display_name=normalized_display_name,
            password_hash=password_hash,
            role=ROLE_ADMIN,
            status=USER_STATUS_ACTIVE,
            must_change_password=must_change_password,
            failed_login_count=0,
        )
        session.add(user)
        await session.flush()
        session.add(
            AdminAuditLog(
                audit_id=str(uuid4()),
                actor_user_id=user_id,
                action="auth.bootstrap_admin_created",
                target_type="user",
                target_id=user_id,
                outcome=AUDIT_OUTCOME_SUCCESS,
                details={
                    "username": normalized_username,
                    "must_change_password": must_change_password,
                },
            )
        )
        await session.commit()
        return user
    except Exception:
        await session.rollback()
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the first EvidenceRAG administrator."
    )
    parser.add_argument("--username", required=True, help="Administrator login name")
    parser.add_argument(
        "--display-name",
        help="Name shown in the UI; defaults to the normalized username",
    )
    parser.add_argument(
        "--require-password-change",
        action="store_true",
        help="Require the administrator to replace this password after first login",
    )
    return parser


async def _run(
    username: str,
    display_name: str,
    password: str,
    *,
    must_change_password: bool,
) -> User:
    session_factory = get_admin_session_factory()
    async with session_factory() as session:
        return await create_initial_admin(
            session,
            username=username,
            display_name=display_name,
            password=password,
            must_change_password=must_change_password,
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    display_name = args.display_name or args.username
    try:
        password = getpass("Administrator password: ")
        confirmation = getpass("Confirm password: ")
    except (EOFError, KeyboardInterrupt):
        print("\nAdministrator creation cancelled.", file=sys.stderr)
        return 2

    if password != confirmation:
        print("Passwords do not match.", file=sys.stderr)
        return 2

    try:
        user = asyncio.run(
            _run(
                args.username,
                display_name,
                password,
                must_change_password=args.require_password_change,
            )
        )
    except (BootstrapAdminExistsError, RuntimeError, ValueError) as exc:
        print(f"Administrator was not created: {exc}", file=sys.stderr)
        return 1

    print(f"Administrator created: {user.username} ({user.user_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
