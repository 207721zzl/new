"""认证服务的状态转换和安全边界测试。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.auth.constants import (
    ROLE_ADMIN,
    ROLE_EMPLOYEE,
    USER_STATUS_ACTIVE,
    USER_STATUS_PENDING,
)
from app.auth.security import hash_password, security_token_matches, verify_password
from app.auth.service import AuthContext, AuthService
from app.config import Settings
from app.db.models import AdminAuditLog, AuthSession, User
from app.errors import (
    AccountUnavailableError,
    AuthenticationRequiredError,
    InvalidCredentialsError,
    PasswordChangeError,
    SessionReplacedError,
    UsernameUnavailableError,
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _user(
    *,
    status: str = USER_STATUS_ACTIVE,
    role: str = ROLE_EMPLOYEE,
    password: str = "correct horse battery staple",
) -> User:
    return User(
        user_id="user-1",
        username="employee.one",
        normalized_username="employee.one",
        display_name="员工一",
        password_hash=hash_password(password),
        role=role,
        status=status,
        must_change_password=False,
        failed_login_count=0,
    )


def _auth_session(**overrides) -> AuthSession:
    now = _now()
    values = {
        "session_id": "session-1",
        "user_id": "user-1",
        "token_hash": "unused",
        "csrf_token_hash": "unused",
        "expires_at": now + timedelta(hours=1),
        "last_seen_at": now,
        "created_at": now,
    }
    values.update(overrides)
    return AuthSession(**values)


class FakeResult:
    def __init__(self, row, *, rowcount: int = 0) -> None:
        self.row = row
        self.rowcount = rowcount

    def first(self):
        return self.row


class FakeSession:
    def __init__(
        self,
        *,
        scalar_results=(),
        execute_results=(),
        execute_rowcounts=(),
    ) -> None:
        self.scalar_results = list(scalar_results)
        self.execute_results = list(execute_results)
        self.execute_rowcounts = list(execute_rowcounts)
        self.added = []
        self.executed = []
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    async def scalar(self, _statement):
        return self.scalar_results.pop(0)

    async def execute(self, statement):
        self.executed.append(statement)
        row = self.execute_results.pop(0) if self.execute_results else None
        rowcount = self.execute_rowcounts.pop(0) if self.execute_rowcounts else 0
        return FakeResult(row, rowcount=rowcount)

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


def test_registration_creates_a_pending_employee_and_audit_event() -> None:
    session = FakeSession(scalar_results=[None])
    service = AuthService(session, Settings(_env_file=None))

    user = asyncio.run(
        service.register(
            username="Employee.One",
            display_name="员工一",
            password="correct horse battery staple",
        )
    )

    assert user.username == "employee.one"
    assert user.role == ROLE_EMPLOYEE
    assert user.status == USER_STATUS_PENDING
    assert verify_password("correct horse battery staple", user.password_hash)
    assert isinstance(session.added[0], User)
    assert isinstance(session.added[1], AdminAuditLog)
    assert session.flush_count == 1
    assert session.commit_count == 1


def test_registration_rejects_a_normalized_duplicate() -> None:
    session = FakeSession(scalar_results=["existing-user"])
    service = AuthService(session, Settings(_env_file=None))

    with pytest.raises(UsernameUnavailableError):
        asyncio.run(
            service.register(
                username="Employee.One",
                display_name="员工一",
                password="correct horse battery staple",
            )
        )

    assert session.added == []
    assert session.rollback_count == 1


def test_active_user_login_issues_hashed_server_side_session() -> None:
    user = _user(role=ROLE_ADMIN)
    session = FakeSession(scalar_results=[user])
    service = AuthService(session, Settings(_env_file=None))

    issued = asyncio.run(
        service.login(
            username="Employee.One",
            password="correct horse battery staple",
            ip_address="127.0.0.1",
            user_agent="test-agent",
        )
    )

    assert issued.user is user
    assert isinstance(session.added[0], AuthSession)
    assert issued.auth_session.token_hash != issued.session_token
    assert security_token_matches(
        issued.session_token,
        issued.auth_session.token_hash,
    )
    assert security_token_matches(
        issued.csrf_token,
        issued.auth_session.csrf_token_hash,
    )
    assert user.failed_login_count == 0
    assert user.last_login_at is not None
    assert len(session.executed) == 1
    assert session.commit_count == 1


def test_login_revokes_previous_sessions_and_records_security_audit() -> None:
    user = _user()
    session = FakeSession(scalar_results=[user], execute_rowcounts=[2])

    asyncio.run(
        AuthService(session, Settings(_env_file=None)).login(
            username=user.username,
            password="correct horse battery staple",
            ip_address="203.0.113.10",
            user_agent="new-device",
        )
    )

    assert isinstance(session.added[0], AuthSession)
    assert isinstance(session.added[1], AdminAuditLog)
    assert session.added[1].action == "auth.session_replaced"
    assert session.added[1].details == {"revoked_sessions": 2}


def test_failed_login_updates_counter_and_locks_at_configured_threshold() -> None:
    user = _user()
    user.failed_login_count = 1
    session = FakeSession(scalar_results=[user])
    settings = Settings(
        _env_file=None,
        auth_login_max_failures=2,
        auth_login_lock_minutes=10,
    )

    with pytest.raises(InvalidCredentialsError):
        asyncio.run(
            AuthService(session, settings).login(
                username=user.username,
                password="definitely incorrect",
                ip_address=None,
                user_agent=None,
            )
        )

    assert user.failed_login_count == 2
    assert user.locked_until is not None
    assert session.commit_count == 1


def test_pending_user_cannot_login_even_with_correct_password() -> None:
    user = _user(status=USER_STATUS_PENDING)
    session = FakeSession(scalar_results=[user])

    with pytest.raises(AccountUnavailableError):
        asyncio.run(
            AuthService(session, Settings(_env_file=None)).login(
                username=user.username,
                password="correct horse battery staple",
                ip_address=None,
                user_agent=None,
            )
        )

    assert session.added == []
    assert session.rollback_count == 1


def test_authenticate_rejects_expired_session_and_revokes_it() -> None:
    user = _user()
    auth_session = _auth_session(expires_at=_now() - timedelta(seconds=1))
    session = FakeSession(execute_results=[(auth_session, user)])

    with pytest.raises(AuthenticationRequiredError):
        asyncio.run(
            AuthService(session, Settings(_env_file=None)).authenticate("raw-token")
        )

    assert auth_session.revoked_at is not None
    assert session.commit_count == 1


def test_authenticate_reports_when_a_new_login_replaced_the_session() -> None:
    user = _user()
    auth_session = _auth_session(revoked_at=_now() - timedelta(seconds=1))
    session = FakeSession(
        scalar_results=["replacement-session"],
        execute_results=[(auth_session, user)],
    )

    with pytest.raises(SessionReplacedError):
        asyncio.run(
            AuthService(session, Settings(_env_file=None)).authenticate("old-token")
        )


def test_csrf_token_must_match_the_authenticated_session() -> None:
    from app.auth.security import generate_security_token, hash_security_token

    token = generate_security_token()
    context = AuthContext(
        user=_user(),
        auth_session=_auth_session(csrf_token_hash=hash_security_token(token)),
    )

    assert AuthService.csrf_matches(
        context,
        cookie_token=token,
        header_token=token,
    )
    assert not AuthService.csrf_matches(
        context,
        cookie_token=token,
        header_token="wrong-token",
    )


def test_password_change_rejects_reuse_and_revokes_sessions_on_success() -> None:
    user = _user()
    auth_session = _auth_session()
    context = AuthContext(user=user, auth_session=auth_session)

    rejected_session = FakeSession()
    with pytest.raises(PasswordChangeError):
        asyncio.run(
            AuthService(rejected_session, Settings(_env_file=None)).change_password(
                context,
                current_password="correct horse battery staple",
                new_password="correct horse battery staple",
            )
        )

    successful_session = FakeSession()
    asyncio.run(
        AuthService(successful_session, Settings(_env_file=None)).change_password(
            context,
            current_password="correct horse battery staple",
            new_password="an entirely different secure password",
        )
    )

    assert verify_password(
        "an entirely different secure password",
        user.password_hash,
    )
    assert len(successful_session.executed) == 1
    assert isinstance(successful_session.added[0], AdminAuditLog)
    assert successful_session.commit_count == 1
