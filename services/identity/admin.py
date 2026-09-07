"""管理员账号、审计和知识库文档管理服务。"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
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
from services.identity.models import (
    AdminAuditLog,
    AuthSession,
    User,
)
from app.errors import (
    ManagedUserNotFoundError,
    ProtectedAdministratorError,
    UsernameUnavailableError,
)
from app.logging_config import get_logger


logger = get_logger("admin.service")


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class AdminService:
    """在请求事务内执行管理员敏感操作并写入审计记录。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _add_audit(
        self,
        *,
        actor_user_id: str,
        action: str,
        target_type: str,
        target_id: str | None,
        request_id: str | None,
        ip_address: str | None,
        details: dict[str, Any],
    ) -> None:
        self.session.add(
            AdminAuditLog(
                audit_id=str(uuid4()),
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                outcome=AUDIT_OUTCOME_SUCCESS,
                request_id=request_id,
                ip_address=(ip_address or "")[:45] or None,
                details=details,
            )
        )

    async def list_users(
        self,
        *,
        search: str | None,
        role: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[User], int]:
        conditions = [User.user_id != LEGACY_USER_ID]
        if search and search.strip():
            needle = search.strip()
            conditions.append(
                or_(
                    User.username.contains(needle, autoescape=True),
                    User.display_name.contains(needle, autoescape=True),
                )
            )
        if role is not None:
            conditions.append(User.role == role)
        if status is not None:
            conditions.append(User.status == status)

        total = int(
            await self.session.scalar(select(func.count()).select_from(User).where(*conditions))
            or 0
        )
        users = await self.session.scalars(
            select(User)
            .where(*conditions)
            .order_by(
                case((User.status == "pending", 0), else_=1),
                User.created_at.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        return list(users), total

    async def create_user(
        self,
        *,
        actor_user_id: str,
        username: str,
        display_name: str,
        password: str,
        role: str,
        status: str,
        must_change_password: bool,
        request_id: str | None,
        ip_address: str | None,
    ) -> User:
        normalized_username = validate_username(username)
        normalized_display_name = validate_display_name(display_name)
        password_hash = await asyncio.to_thread(hash_password, password)
        now = _utcnow()
        user = User(
            user_id=str(uuid4()),
            username=normalized_username,
            normalized_username=normalized_username,
            display_name=normalized_display_name,
            password_hash=password_hash,
            role=role,
            status=status,
            must_change_password=must_change_password,
            failed_login_count=0,
            created_by_user_id=actor_user_id,
            created_at=now,
            updated_at=now,
        )
        try:
            self.session.add(user)
            await self.session.flush()
            self._add_audit(
                actor_user_id=actor_user_id,
                action="admin.user_created",
                target_type="user",
                target_id=user.user_id,
                request_id=request_id,
                ip_address=ip_address,
                details={
                    "username": normalized_username,
                    "role": role,
                    "status": status,
                    "must_change_password": must_change_password,
                },
            )
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise UsernameUnavailableError() from exc
        return user

    async def _get_managed_user(self, user_id: str) -> User:
        if user_id == LEGACY_USER_ID:
            raise ManagedUserNotFoundError()
        user = await self.session.scalar(
            select(User).where(User.user_id == user_id).with_for_update()
        )
        if user is None:
            raise ManagedUserNotFoundError()
        return user

    async def _protect_administrator(
        self,
        *,
        actor_user_id: str,
        user: User,
        new_role: str,
        new_status: str,
    ) -> None:
        removes_active_admin = (
            user.role == ROLE_ADMIN
            and user.status == USER_STATUS_ACTIVE
            and (new_role != ROLE_ADMIN or new_status != USER_STATUS_ACTIVE)
        )
        changes_actor_access = user.user_id == actor_user_id and (
            new_role != user.role or new_status != user.status
        )
        if changes_actor_access:
            raise ProtectedAdministratorError()
        if not removes_active_admin:
            return
        active_admin_count = int(
            await self.session.scalar(
                select(func.count()).select_from(User).where(
                    User.role == ROLE_ADMIN,
                    User.status == USER_STATUS_ACTIVE,
                )
            )
            or 0
        )
        if active_admin_count <= 1:
            raise ProtectedAdministratorError()

    async def update_user(
        self,
        *,
        actor_user_id: str,
        user_id: str,
        display_name: str | None,
        role: str | None,
        status: str | None,
        request_id: str | None,
        ip_address: str | None,
    ) -> User:
        user = await self._get_managed_user(user_id)
        new_role = role if role is not None else user.role
        new_status = status if status is not None else user.status
        await self._protect_administrator(
            actor_user_id=actor_user_id,
            user=user,
            new_role=new_role,
            new_status=new_status,
        )
        before = {
            "display_name": user.display_name,
            "role": user.role,
            "status": user.status,
        }
        if display_name is not None:
            user.display_name = validate_display_name(display_name)
        user.role = new_role
        user.status = new_status
        user.updated_at = _utcnow()
        if new_status == USER_STATUS_ACTIVE:
            user.failed_login_count = 0
            user.locked_until = None
        if new_status != USER_STATUS_ACTIVE or new_role != before["role"]:
            await self.session.execute(
                update(AuthSession)
                .where(
                    AuthSession.user_id == user.user_id,
                    AuthSession.revoked_at.is_(None),
                )
                .values(revoked_at=_utcnow())
            )
        self._add_audit(
            actor_user_id=actor_user_id,
            action="admin.user_updated",
            target_type="user",
            target_id=user.user_id,
            request_id=request_id,
            ip_address=ip_address,
            details={
                "before": before,
                "after": {
                    "display_name": user.display_name,
                    "role": user.role,
                    "status": user.status,
                },
            },
        )
        await self.session.commit()
        return user

    async def reset_password(
        self,
        *,
        actor_user_id: str,
        user_id: str,
        temporary_password: str,
        request_id: str | None,
        ip_address: str | None,
    ) -> User:
        user = await self._get_managed_user(user_id)
        user.password_hash = await asyncio.to_thread(hash_password, temporary_password)
        user.password_changed_at = _utcnow()
        user.updated_at = user.password_changed_at
        user.must_change_password = True
        user.failed_login_count = 0
        user.locked_until = None
        await self.session.execute(
            update(AuthSession)
            .where(
                AuthSession.user_id == user.user_id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=_utcnow())
        )
        self._add_audit(
            actor_user_id=actor_user_id,
            action="admin.user_password_reset",
            target_type="user",
            target_id=user.user_id,
            request_id=request_id,
            ip_address=ip_address,
            details={"username": user.username, "must_change_password": True},
        )
        await self.session.commit()
        return user

    async def list_audit_logs(
        self,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        total = int(await self.session.scalar(select(func.count()).select_from(AdminAuditLog)) or 0)
        rows = await self.session.execute(
            select(AdminAuditLog, User.username)
            .outerjoin(User, User.user_id == AdminAuditLog.actor_user_id)
            .order_by(AdminAuditLog.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return [
            {
                "audit_id": audit.audit_id,
                "actor_user_id": audit.actor_user_id,
                "actor_username": actor_username,
                "action": audit.action,
                "target_type": audit.target_type,
                "target_id": audit.target_id,
                "outcome": audit.outcome,
                "details": audit.details,
                "created_at": audit.created_at,
            }
            for audit, actor_username in rows.all()
        ], total
