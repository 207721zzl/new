"""管理员专用账号、审计与知识库管理 API。"""

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.schemas import (
    AdminAuditList,
    AdminAuditSummary,
    AdminPasswordResetRequest,
    AdminUserCreate,
    AdminUserList,
    AdminUserResult,
    AdminUserSummary,
    AdminUserUpdate,
)
from services.identity.admin import AdminService
from services.identity.dependencies import require_admin, require_admin_csrf
from app.auth.schemas import UserRole, UserStatus
from services.identity.service import AuthContext
from services.identity.db import get_admin_session
from app.logging_config import request_id_context


router = APIRouter(prefix="/api/v1/admin", tags=["administration"])


def get_admin_service(
    session: AsyncSession = Depends(get_admin_session),
) -> AdminService:
    return AdminService(session)


def _request_metadata(request: Request) -> dict[str, str | None]:
    return {
        "request_id": request_id_context.get(),
        "ip_address": request.client.host if request.client is not None else None,
    }


@router.get("/users", response_model=AdminUserList)
async def list_users(
    search: str | None = Query(default=None, max_length=128),
    role: UserRole | None = None,
    status_filter: UserStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _context: AuthContext = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
) -> AdminUserList:
    users, total = await service.list_users(
        search=search,
        role=role,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return AdminUserList(
        items=[AdminUserSummary.model_validate(user) for user in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/users",
    response_model=AdminUserResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    payload: AdminUserCreate,
    request: Request,
    context: AuthContext = Depends(require_admin_csrf),
    service: AdminService = Depends(get_admin_service),
) -> AdminUserResult:
    user = await service.create_user(
        actor_user_id=context.user.user_id,
        username=payload.username,
        display_name=payload.display_name,
        password=payload.password.get_secret_value(),
        role=payload.role,
        status=payload.status,
        must_change_password=payload.must_change_password,
        **_request_metadata(request),
    )
    return AdminUserResult(user=AdminUserSummary.model_validate(user))


@router.patch("/users/{user_id}", response_model=AdminUserResult)
async def update_user(
    user_id: str,
    payload: AdminUserUpdate,
    request: Request,
    context: AuthContext = Depends(require_admin_csrf),
    service: AdminService = Depends(get_admin_service),
) -> AdminUserResult:
    user = await service.update_user(
        actor_user_id=context.user.user_id,
        user_id=user_id,
        display_name=payload.display_name,
        role=payload.role,
        status=payload.status,
        **_request_metadata(request),
    )
    return AdminUserResult(user=AdminUserSummary.model_validate(user))


@router.post("/users/{user_id}/reset-password", response_model=AdminUserResult)
async def reset_user_password(
    user_id: str,
    payload: AdminPasswordResetRequest,
    request: Request,
    context: AuthContext = Depends(require_admin_csrf),
    service: AdminService = Depends(get_admin_service),
) -> AdminUserResult:
    user = await service.reset_password(
        actor_user_id=context.user.user_id,
        user_id=user_id,
        temporary_password=payload.temporary_password.get_secret_value(),
        **_request_metadata(request),
    )
    return AdminUserResult(user=AdminUserSummary.model_validate(user))


@router.get("/audit-logs", response_model=AdminAuditList)
async def list_audit_logs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _context: AuthContext = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
) -> AdminAuditList:
    items, total = await service.list_audit_logs(limit=limit, offset=offset)
    return AdminAuditList(
        items=[AdminAuditSummary.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
