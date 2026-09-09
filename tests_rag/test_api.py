from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.auth.constants import ROLE_ADMIN, ROLE_EMPLOYEE, USER_STATUS_ACTIVE
from app.auth.dependencies import get_optional_auth_context
from app.auth.service import AuthContext
from app.db.models import AuthSession, User


def _page_context(role: str) -> AuthContext:
    now = datetime.now(UTC).replace(tzinfo=None)
    user = User(
        user_id=f"{role}-1",
        username=role,
        normalized_username=role,
        display_name="系统管理员" if role == ROLE_ADMIN else "试点员工",
        password_hash="not-used",
        role=role,
        status=USER_STATUS_ACTIVE,
        must_change_password=False,
        failed_login_count=0,
    )
    return AuthContext(
        user=user,
        auth_session=AuthSession(
            session_id="session-1",
            user_id=user.user_id,
            token_hash="not-used",
            csrf_token_hash="not-used",
            expires_at=now + timedelta(hours=1),
            last_seen_at=now,
            created_at=now,
        ),
    )


def test_health_root_and_openapi(monkeypatch):
    monkeypatch.setenv("MODEL_PRELOAD_ENABLED", "false")
    monkeypatch.setenv("MODEL_WARMUP_ENABLED", "false")
    from app.main import app

    app.dependency_overrides[get_optional_auth_context] = lambda: None
    with TestClient(app, follow_redirects=False) as client:
        health = client.get("/api/v1/health")
        root = client.get("/")
        login = client.get("/login")
        admin = client.get("/admin")
        schema = client.get("/openapi.json").json()
    app.dependency_overrides.clear()

    assert health.status_code == 200
    assert health.json()["service"] == "EvidenceRAG"
    assert root.status_code == 303
    assert root.headers["location"] == "/login?next=%2F"
    assert login.status_code == 200
    assert "登录工作台" in login.text
    assert "同一账号仅允许一处在线" in login.text
    assert admin.status_code == 303
    assert admin.headers["location"] == "/login?next=%2Fadmin"
    assert "/api/v1/knowledge/uploads" in schema["paths"]
    assert "/api/v1/retrieval/search" in schema["paths"]
    assert "/api/v1/auth/register" in schema["paths"]
    assert "/api/v1/auth/login" in schema["paths"]
    assert "/api/v1/auth/me" in schema["paths"]
    assert "/api/v1/admin/users" in schema["paths"]
    assert "/api/v1/admin/knowledge/documents/{document_id}" in schema["paths"]
    assert "/api/v1/admin/pilot/overview" in schema["paths"]


def test_page_routes_are_strictly_split_by_role(monkeypatch):
    monkeypatch.setenv("MODEL_PRELOAD_ENABLED", "false")
    from app.main import app

    app.dependency_overrides[get_optional_auth_context] = lambda: _page_context(ROLE_ADMIN)
    with TestClient(app, follow_redirects=False) as client:
        admin_root = client.get("/")
        admin_page = client.get("/admin")

    app.dependency_overrides[get_optional_auth_context] = lambda: _page_context(ROLE_EMPLOYEE)
    with TestClient(app, follow_redirects=False) as client:
        employee_root = client.get("/")
        employee_admin = client.get("/admin")
    app.dependency_overrides.clear()

    assert admin_root.status_code == 303
    assert admin_root.headers["location"] == "/admin"
    assert admin_page.status_code == 200
    assert "管理控制台" in admin_page.text
    assert employee_root.status_code == 200
    assert "文档知识问答" in employee_root.text
    assert employee_admin.status_code == 303
    assert employee_admin.headers["location"] == "/"


def test_business_and_management_apis_require_login(monkeypatch):
    monkeypatch.setenv("MODEL_PRELOAD_ENABLED", "false")
    from app.main import app

    with TestClient(app) as client:
        conversations = client.get("/api/v1/conversations")
        run = client.post("/api/v1/runs", json={"question": "test"})
        documents = client.get("/api/v1/knowledge/documents")
        users = client.get("/api/v1/admin/users")

    assert conversations.status_code == 401
    assert run.status_code == 401
    assert documents.status_code == 401
    assert users.status_code == 401
    assert conversations.headers["cache-control"] == "no-store"
    assert conversations.headers["x-content-type-options"] == "nosniff"
    assert conversations.headers["x-frame-options"] == "DENY"
