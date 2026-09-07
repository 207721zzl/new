"""注册、登录、会话校验、注销和密码变更服务。"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.constants import (
    AUDIT_OUTCOME_SUCCESS,
    ROLE_EMPLOYEE,
    USER_STATUS_ACTIVE,
    USER_STATUS_PENDING,
)
from app.auth.security import (
    generate_security_token,
    hash_password,
    hash_security_token,
    password_needs_rehash,
    security_token_matches,
    validate_display_name,
    validate_username,
    verify_password,
)
from app.config import Settings
from app.db.models import AdminAuditLog, AuthSession, User
from app.errors import (
    AccountLockedError,
    AccountUnavailableError,
    AuthenticationRequiredError,
    InvalidCredentialsError,
    PasswordChangeError,
    RegistrationDisabledError,
    UsernameUnavailableError,
)


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """登录成功后交给 HTTP 层设置 Cookie 的一次性结果。"""

    user: User
    auth_session: AuthSession
    session_token: str
    csrf_token: str


@dataclass(frozen=True, slots=True)
class AuthContext:
    """一次已认证请求对应的用户和服务端会话。"""

    user: User
    auth_session: AuthSession


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """为不存在的用户名执行同类密码计算，降低时序枚举风险。"""
    return hash_password("evidence-rag-dummy-password-not-for-login")


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class AuthService:
    """在单个数据库事务边界内维护账号和登录会话。"""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def register(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
    ) -> User:
        """创建待管理员审批的普通员工账号。"""
        if not self.settings.auth_registration_enabled:
            raise RegistrationDisabledError()

        normalized_username = validate_username(username)
        normalized_display_name = validate_display_name(display_name)
        password_hash = await asyncio.to_thread(hash_password, password)
        existing_user_id = await self.session.scalar(
            select(User.user_id)
            .where(User.normalized_username == normalized_username)
            .limit(1)
        )
        if existing_user_id is not None:
            await self.session.rollback()
            raise UsernameUnavailableError()

        user_id = str(uuid4())
        user = User(
            user_id=user_id,
            username=normalized_username,
            normalized_username=normalized_username,
            display_name=normalized_display_name,
            password_hash=password_hash,
            role=ROLE_EMPLOYEE,
            status=USER_STATUS_PENDING,
            must_change_password=False,
            failed_login_count=0,
        )
        try:
            self.session.add(user)
            await self.session.flush()
            self.session.add(
                AdminAuditLog(
                    audit_id=str(uuid4()),
                    actor_user_id=user_id,
                    action="auth.user_registered",
                    target_type="user",
                    target_id=user_id,
                    outcome=AUDIT_OUTCOME_SUCCESS,
                    details={"username": normalized_username},
                )
            )
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise UsernameUnavailableError() from exc
        return user

    async def login(
        self,
        *,
        username: str,
        password: str,
        ip_address: str | None,
        user_agent: str | None,
    ) -> IssuedSession:
        """校验凭据、执行失败锁定并签发可撤销的服务端会话。"""
        try:
            normalized_username = validate_username(username)
        except ValueError:
            await asyncio.to_thread(
                verify_password,
                password,
                _dummy_password_hash(),
            )
            raise InvalidCredentialsError() from None

        user = await self.session.scalar(
            select(User)
            .where(User.normalized_username == normalized_username)
            .with_for_update()
        )
        password_hash = user.password_hash if user is not None else _dummy_password_hash()
        password_matches = await asyncio.to_thread(
            verify_password,
            password,
            password_hash,
        )
        if user is None:
            raise InvalidCredentialsError()

        now = _utcnow()
        if user.locked_until is not None and user.locked_until > now:
            raise AccountLockedError()
        if user.locked_until is not None:
            user.locked_until = None
            user.failed_login_count = 0

        if not password_matches:
            user.failed_login_count += 1
            if user.failed_login_count >= self.settings.auth_login_max_failures:
                user.locked_until = now + timedelta(
                    minutes=self.settings.auth_login_lock_minutes
                )
            await self.session.commit()
            raise InvalidCredentialsError()

        if user.status != USER_STATUS_ACTIVE:
            await self.session.rollback()
            raise AccountUnavailableError()

        if password_needs_rehash(user.password_hash):
            user.password_hash = await asyncio.to_thread(hash_password, password)
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now

        session_token = generate_security_token()
        csrf_token = generate_security_token()
        auth_session = AuthSession(
            session_id=str(uuid4()),
            user_id=user.user_id,
            token_hash=hash_security_token(session_token),
            csrf_token_hash=hash_security_token(csrf_token),
            expires_at=now
            + timedelta(hours=self.settings.auth_session_absolute_hours),
            last_seen_at=now,
            ip_address=(ip_address or "")[:45] or None,
            user_agent=(user_agent or "")[:512] or None,
            created_at=now,
        )
        self.session.add(auth_session)
        await self.session.commit()
        return IssuedSession(
            user=user,
            auth_session=auth_session,
            session_token=session_token,
            csrf_token=csrf_token,
        )

    async def authenticate(self, session_token: str | None) -> AuthContext:
        """解析 Cookie 会话，并执行绝对超时、空闲超时和账号状态校验。"""
        if not session_token:
            raise AuthenticationRequiredError()
        token_hash = hash_security_token(session_token)
        row = (
            await self.session.execute(
                select(AuthSession, User)
                .join(User, User.user_id == AuthSession.user_id)
                .where(AuthSession.token_hash == token_hash)
                .limit(1)
            )
        ).first()
        if row is None:
            raise AuthenticationRequiredError()

        auth_session, user = row
        now = _utcnow()
        idle_deadline = now - timedelta(
            minutes=self.settings.auth_session_idle_minutes
        )
        invalid_session = (
            auth_session.revoked_at is not None
            or auth_session.expires_at <= now
            or auth_session.last_seen_at <= idle_deadline
            or user.status != USER_STATUS_ACTIVE
        )
        if invalid_session:
            if auth_session.revoked_at is None:
                auth_session.revoked_at = now
                await self.session.commit()
            raise AuthenticationRequiredError()

        touch_before = now - timedelta(
            seconds=self.settings.auth_session_touch_interval_seconds
        )
        if auth_session.last_seen_at <= touch_before:
            auth_session.last_seen_at = now
            await self.session.commit()
        return AuthContext(user=user, auth_session=auth_session)

    @staticmethod
    def csrf_matches(
        context: AuthContext,
        *,
        cookie_token: str | None,
        header_token: str | None,
    ) -> bool:
        """同时校验双提交值和服务器保存的会话绑定摘要。"""
        if not cookie_token or not header_token:
            return False
        if not security_token_matches(header_token, context.auth_session.csrf_token_hash):
            return False
        return security_token_matches(cookie_token, context.auth_session.csrf_token_hash)

    async def logout(self, context: AuthContext) -> None:
        """幂等撤销当前服务端会话。"""
        if context.auth_session.revoked_at is None:
            context.auth_session.revoked_at = _utcnow()
            await self.session.commit()

    async def change_password(
        self,
        context: AuthContext,
        *,
        current_password: str,
        new_password: str,
    ) -> None:
        """修改密码并撤销该用户的全部现有登录会话。"""
        current_matches = await asyncio.to_thread(
            verify_password,
            current_password,
            context.user.password_hash,
        )
        reuses_current = await asyncio.to_thread(
            verify_password,
            new_password,
            context.user.password_hash,
        )
        if not current_matches or reuses_current:
            raise PasswordChangeError()

        now = _utcnow()
        context.user.password_hash = await asyncio.to_thread(
            hash_password,
            new_password,
        )
        context.user.password_changed_at = now
        context.user.must_change_password = False
        await self.session.execute(
            update(AuthSession)
            .where(
                AuthSession.user_id == context.user.user_id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        self.session.add(
            AdminAuditLog(
                audit_id=str(uuid4()),
                actor_user_id=context.user.user_id,
                action="auth.password_changed",
                target_type="user",
                target_id=context.user.user_id,
                outcome=AUDIT_OUTCOME_SUCCESS,
                details={},
            )
        )
        await self.session.commit()
