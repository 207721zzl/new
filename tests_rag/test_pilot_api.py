"""试点看板只允许管理员访问，清理操作必须通过 CSRF 和确认短语。"""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.auth.constants import ROLE_ADMIN, ROLE_EMPLOYEE, USER_STATUS_ACTIVE
from app.auth.dependencies import get_current_auth_context
from app.auth.security import hash_security_token
from app.auth.service import AuthContext
from app.db.models import AuthSession, User
from app.errors import AppError
from app.pilot.router import get_pilot_service, router
from app.pilot.schemas import (
    PilotAccountSummary,
    PilotCleanupResult,
    PilotFeedbackSummary,
    PilotKnowledgeSummary,
    PilotMaintenancePreview,
    PilotOverview,
    PilotRuntimeSummary,
    PilotTokenSummary,
    PilotWorkloadSummary,
)


CSRF_TOKEN = "pilot-csrf-token"


def _context(role: str) -> AuthContext:
    now = datetime.now(UTC).replace(tzinfo=None)
    user = User(
        user_id=f"{role}-1",
        username=role,
        normalized_username=role,
        display_name="管理员" if role == ROLE_ADMIN else "员工",
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
            csrf_token_hash=hash_security_token(CSRF_TOKEN),
            expires_at=now + timedelta(hours=1),
            last_seen_at=now,
            created_at=now,
        ),
    )


def _workload() -> PilotWorkloadSummary:
    return PilotWorkloadSummary(
        total=1,
        queued=0,
        running=0,
        completed=1,
        failed=0,
        stale=0,
        success_rate=1,
        average_duration_ms=100,
        p95_duration_ms=100,
    )


def _preview() -> PilotMaintenancePreview:
    return PilotMaintenancePreview(
        generated_at=datetime.now(UTC),
        conversation_retention_days=90,
        session_retention_days=30,
        audit_retention_days=365,
        conversations_to_delete=1,
        sessions_to_delete=2,
        audit_logs_to_delete=3,
    )


class FakePilotService:
    def __init__(self) -> None:
        self.cleaned = None

    async def overview(self) -> PilotOverview:
        return PilotOverview(
            generated_at=datetime.now(UTC),
            window_hours=24,
            runtime=PilotRuntimeSummary(
                started_at=datetime.now(UTC),
                uptime_seconds=1,
                requests_total=2,
                responses_4xx=0,
                responses_5xx=0,
                latency_sample_count=2,
                average_latency_ms=10,
                p95_latency_ms=15,
            ),
            accounts=PilotAccountSummary(
                total=2,
                active_admins=1,
                active_employees=1,
                pending=0,
                disabled=0,
                active_sessions=1,
            ),
            knowledge=PilotKnowledgeSummary(
                documents=1,
                parent_chunks=2,
                child_chunks=3,
                indexed_source_bytes=100,
            ),
            runs=_workload(),
            indexing=_workload(),
            feedback=PilotFeedbackSummary(
                positive=1,
                negative=0,
                positive_rate=1,
            ),
            tokens=PilotTokenSummary(
                prompt_tokens=10,
                completion_tokens=20,
                total_tokens=30,
            ),
            disk_free_bytes=1000,
            model_state="ready",
            warnings=[],
        )

    async def maintenance_preview(self) -> PilotMaintenancePreview:
        return _preview()

    async def cleanup(self, **values) -> PilotCleanupResult:
        self.cleaned = values
        return PilotCleanupResult(**_preview().model_dump(), status="completed")


def _app(service: FakePilotService, role: str) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_auth_context] = lambda: _context(role)
    app.dependency_overrides[get_pilot_service] = lambda: service

    @app.exception_handler(AppError)
    async def handle_error(_request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.public_message}},
        )

    return app


def test_employee_cannot_read_pilot_dashboard() -> None:
    with TestClient(_app(FakePilotService(), ROLE_EMPLOYEE)) as client:
        response = client.get("/api/v1/admin/pilot/overview")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_admin_can_read_dashboard_and_preview_but_cleanup_requires_csrf() -> None:
    service = FakePilotService()
    with TestClient(_app(service, ROLE_ADMIN)) as client:
        overview = client.get("/api/v1/admin/pilot/overview")
        preview = client.get("/api/v1/admin/pilot/maintenance")
        rejected = client.post(
            "/api/v1/admin/pilot/maintenance/cleanup",
            json={"confirmation": "PURGE_EXPIRED_PILOT_DATA"},
        )
        client.cookies.set("evidence_rag_csrf", CSRF_TOKEN)
        accepted = client.post(
            "/api/v1/admin/pilot/maintenance/cleanup",
            headers={"X-CSRF-Token": CSRF_TOKEN},
            json={"confirmation": "PURGE_EXPIRED_PILOT_DATA"},
        )

    assert overview.status_code == 200
    assert preview.status_code == 200
    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert service.cleaned["actor_user_id"] == "admin-1"


def test_cleanup_rejects_incorrect_confirmation() -> None:
    with TestClient(_app(FakePilotService(), ROLE_ADMIN)) as client:
        client.cookies.set("evidence_rag_csrf", CSRF_TOKEN)
        response = client.post(
            "/api/v1/admin/pilot/maintenance/cleanup",
            headers={"X-CSRF-Token": CSRF_TOKEN},
            json={"confirmation": "DELETE"},
        )

    assert response.status_code == 422
