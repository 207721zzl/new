"""管理员 HTTP 接口的角色和 CSRF 边界测试。"""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.admin.router import get_admin_service, router
from app.admin.schemas import KnowledgeDocumentDeleted
from app.auth.constants import ROLE_ADMIN, ROLE_EMPLOYEE, USER_STATUS_ACTIVE
from app.auth.dependencies import get_current_auth_context
from app.auth.security import hash_security_token
from app.auth.service import AuthContext
from app.db.models import AuthSession, User
from app.errors import AppError


CSRF_TOKEN = "admin-api-csrf-token"


def _user(*, role: str = ROLE_ADMIN, user_id: str = "admin-1") -> User:
    now = datetime.now(UTC).replace(tzinfo=None)
    return User(
        user_id=user_id,
        username="admin" if role == ROLE_ADMIN else "employee",
        normalized_username="admin" if role == ROLE_ADMIN else "employee",
        display_name="系统管理员" if role == ROLE_ADMIN else "员工一",
        password_hash="not-used",
        role=role,
        status=USER_STATUS_ACTIVE,
        must_change_password=False,
        failed_login_count=0,
        created_at=now,
        updated_at=now,
    )


def _context(*, role: str = ROLE_ADMIN) -> AuthContext:
    now = datetime.now(UTC).replace(tzinfo=None)
    user = _user(role=role)
    return AuthContext(
        user=user,
        auth_session=AuthSession(
            session_id="session-1",
            user_id=user.user_id,
            token_hash="not-used",
            csrf_token_hash=hash_security_token(CSRF_TOKEN),
            expires_at=now + timedelta(hours=1),
            last_seen_at=now,
            created_at=now,
        ),
    )


class FakeAdminService:
    def __init__(self) -> None:
        self.user = _user(role=ROLE_EMPLOYEE, user_id="employee-1")
        self.created = None
        self.updated = None
        self.reset = None
        self.deleted = None

    async def list_users(self, **_values):
        return [self.user], 1

    async def create_user(self, **values):
        self.created = values
        return self.user

    async def update_user(self, **values):
        self.updated = values
        return self.user

    async def reset_password(self, **values):
        self.reset = values
        return self.user

    async def list_audit_logs(self, **_values):
        return [], 0

    async def delete_knowledge_document(self, **values):
        self.deleted = values
        return KnowledgeDocumentDeleted(
            document_id=values["document_id"],
            title="员工手册",
            parent_chunk_count=2,
            child_chunk_count=5,
        )


def _build_app(service: FakeAdminService, *, role: str = ROLE_ADMIN) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_auth_context] = lambda: _context(role=role)
    app.dependency_overrides[get_admin_service] = lambda: service

    @app.exception_handler(AppError)
    async def handle_error(_request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.public_message}},
        )

    return app


def test_employee_cannot_list_admin_users() -> None:
    with TestClient(_build_app(FakeAdminService(), role=ROLE_EMPLOYEE)) as client:
        response = client.get("/api/v1/admin/users")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_admin_user_write_requires_csrf_and_never_returns_password() -> None:
    service = FakeAdminService()
    with TestClient(_build_app(service)) as client:
        client.cookies.set("evidence_rag_csrf", CSRF_TOKEN)
        rejected = client.post(
            "/api/v1/admin/users",
            json={
                "username": "employee.one",
                "display_name": "员工一",
                "password": "temporary password value",
            },
        )
        accepted = client.post(
            "/api/v1/admin/users",
            headers={"X-CSRF-Token": CSRF_TOKEN},
            json={
                "username": "employee.one",
                "display_name": "员工一",
                "password": "temporary password value",
            },
        )

    assert rejected.status_code == 403
    assert accepted.status_code == 201
    assert "password_hash" not in accepted.text
    assert "temporary password value" not in accepted.text
    assert service.created["actor_user_id"] == "admin-1"


def test_admin_can_update_reset_and_delete_with_bound_csrf() -> None:
    service = FakeAdminService()
    with TestClient(_build_app(service)) as client:
        client.cookies.set("evidence_rag_csrf", CSRF_TOKEN)
        headers = {"X-CSRF-Token": CSRF_TOKEN}
        updated = client.patch(
            "/api/v1/admin/users/employee-1",
            headers=headers,
            json={"status": "disabled"},
        )
        reset = client.post(
            "/api/v1/admin/users/employee-1/reset-password",
            headers=headers,
            json={"temporary_password": "another temporary password"},
        )
        deleted = client.delete(
            "/api/v1/admin/knowledge/documents/doc-1",
            headers=headers,
        )

    assert updated.status_code == 200
    assert reset.status_code == 200
    assert deleted.status_code == 200
    assert service.updated["status"] == "disabled"
    assert service.reset["temporary_password"] == "another temporary password"
    assert service.deleted["document_id"] == "doc-1"
