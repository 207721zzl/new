"""认证 HTTP 接口、Cookie 和 CSRF 行为测试。"""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.auth.constants import (
    ROLE_ADMIN,
    ROLE_EMPLOYEE,
    USER_STATUS_ACTIVE,
    USER_STATUS_PENDING,
)
from app.auth.dependencies import get_auth_service
from app.auth.router import router
from app.auth.service import AuthContext, IssuedSession
from app.auth.security import hash_security_token
from app.config import Settings, get_settings
from app.db.models import AuthSession, User
from app.errors import AppError, AuthenticationRequiredError


SESSION_TOKEN = "session-token-for-api-tests"
CSRF_TOKEN = "csrf-token-for-api-tests"


def _user(
    *,
    status: str = USER_STATUS_ACTIVE,
    role: str = ROLE_ADMIN,
) -> User:
    return User(
        user_id="admin-1",
        username="admin",
        normalized_username="admin",
        display_name="系统管理员",
        password_hash="not-used-by-api-test",
        role=role,
        status=status,
        must_change_password=False,
        failed_login_count=0,
    )


def _issued_session() -> IssuedSession:
    now = datetime.now(UTC).replace(tzinfo=None)
    user = _user()
    auth_session = AuthSession(
        session_id="session-1",
        user_id=user.user_id,
        token_hash=hash_security_token(SESSION_TOKEN),
        csrf_token_hash=hash_security_token(CSRF_TOKEN),
        expires_at=now + timedelta(hours=12),
        last_seen_at=now,
        created_at=now,
    )
    return IssuedSession(
        user=user,
        auth_session=auth_session,
        session_token=SESSION_TOKEN,
        csrf_token=CSRF_TOKEN,
    )


class FakeAuthService:
    def __init__(self) -> None:
        self.issued = _issued_session()
        self.logout_called = False
        self.changed_passwords = None
        self.registered = None

    async def register(self, **values):
        self.registered = values
        return _user(status=USER_STATUS_PENDING, role=ROLE_EMPLOYEE)

    async def login(self, **_values):
        return self.issued

    async def authenticate(self, token):
        if token != SESSION_TOKEN:
            raise AuthenticationRequiredError()
        return AuthContext(
            user=self.issued.user,
            auth_session=self.issued.auth_session,
        )

    async def logout(self, _context):
        self.logout_called = True

    async def change_password(self, _context, **values):
        self.changed_passwords = values


def _build_app(service: FakeAuthService, settings: Settings | None = None) -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.dependency_overrides[get_auth_service] = lambda: service
    test_app.dependency_overrides[get_settings] = lambda: settings or Settings(
        _env_file=None
    )

    @test_app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.public_message}},
        )

    return test_app


def test_register_creates_pending_account_without_setting_login_cookie() -> None:
    service = FakeAuthService()
    with TestClient(_build_app(service)) as client:
        response = client.post(
            "/api/v1/auth/register",
            headers={"Origin": "http://testserver"},
            json={
                "username": "employee.one",
                "display_name": "员工一",
                "password": "correct horse battery staple",
            },
        )

    assert response.status_code == 201
    assert response.json()["user"]["status"] == USER_STATUS_PENDING
    assert response.json()["user"]["role"] == ROLE_EMPLOYEE
    assert "set-cookie" not in response.headers
    assert service.registered["password"] == "correct horse battery staple"


def test_register_accepts_a_chinese_username() -> None:
    service = FakeAuthService()
    with TestClient(_build_app(service)) as client:
        response = client.post(
            "/api/v1/auth/register",
            headers={"Origin": "http://testserver"},
            json={
                "username": "大臭蛋",
                "display_name": "小帆",
                "password": "correct horse battery staple",
            },
        )

    assert response.status_code == 201
    assert service.registered["username"] == "大臭蛋"


def test_client_config_exposes_only_frontend_auth_settings() -> None:
    service = FakeAuthService()
    settings = Settings(_env_file=None, auth_registration_enabled=False)
    with TestClient(_build_app(service, settings)) as client:
        response = client.get("/api/v1/auth/config")

    assert response.status_code == 200
    assert response.json() == {
        "registration_enabled": False,
        "csrf_cookie_name": "evidence_rag_csrf",
        "password_min_length": 12,
    }
    assert response.headers["cache-control"] == "no-store"


def test_login_sets_httponly_session_and_readable_csrf_cookies() -> None:
    service = FakeAuthService()
    with TestClient(_build_app(service)) as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://testserver"},
            json={"username": "admin", "password": "a valid password value"},
        )
        current = client.get("/api/v1/auth/me")

    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(item for item in cookies if "evidence_rag_session=" in item)
    csrf_cookie = next(item for item in cookies if "evidence_rag_csrf=" in item)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "SameSite=lax" in session_cookie
    assert "Secure" not in session_cookie
    assert current.status_code == 200
    assert current.json()["user_id"] == "admin-1"


def test_forwarded_https_marks_both_cookies_secure() -> None:
    service = FakeAuthService()
    with TestClient(_build_app(service)) as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={
                "Origin": "https://testserver",
                "X-Forwarded-Proto": "https",
            },
            json={"username": "admin", "password": "a valid password value"},
        )

    assert response.status_code == 200
    assert all("Secure" in item for item in response.headers.get_list("set-cookie"))


def test_cross_site_login_is_rejected_before_credentials_are_checked() -> None:
    service = FakeAuthService()
    with TestClient(_build_app(service)) as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={
                "Origin": "https://attacker.example",
                "Sec-Fetch-Site": "cross-site",
            },
            json={"username": "admin", "password": "a valid password value"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"


def test_logout_requires_bound_csrf_header_and_clears_cookies() -> None:
    service = FakeAuthService()
    with TestClient(_build_app(service)) as client:
        client.cookies.set("evidence_rag_session", SESSION_TOKEN)
        client.cookies.set("evidence_rag_csrf", CSRF_TOKEN)
        rejected = client.post("/api/v1/auth/logout")
        accepted = client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": CSRF_TOKEN},
        )

    assert rejected.status_code == 403
    assert accepted.status_code == 204
    assert service.logout_called is True
    assert "evidence_rag_session=\"\"" in accepted.headers.get("set-cookie", "")


def test_change_password_requires_csrf_and_returns_logged_out_state() -> None:
    service = FakeAuthService()
    with TestClient(_build_app(service)) as client:
        client.cookies.set("evidence_rag_session", SESSION_TOKEN)
        client.cookies.set("evidence_rag_csrf", CSRF_TOKEN)
        response = client.post(
            "/api/v1/auth/change-password",
            headers={"X-CSRF-Token": CSRF_TOKEN},
            json={
                "current_password": "correct horse battery staple",
                "new_password": "an entirely different secure password",
            },
        )

    assert response.status_code == 204
    assert service.changed_passwords == {
        "current_password": "correct horse battery staple",
        "new_password": "an entirely different secure password",
    }
