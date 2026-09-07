"""管理员专用试点运行看板和维护接口。"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin, require_admin_csrf
from app.auth.service import AuthContext
from app.config import Settings, get_settings
from app.db.session import get_admin_session
from app.logging_config import request_id_context
from app.pilot.schemas import (
    PilotCleanupRequest,
    PilotCleanupResult,
    PilotMaintenancePreview,
    PilotOverview,
)
from app.pilot.service import PilotService


router = APIRouter(prefix="/api/v1/admin/pilot", tags=["pilot-operations"])


def get_pilot_service(
    session: AsyncSession = Depends(get_admin_session),
    settings: Settings = Depends(get_settings),
) -> PilotService:
    return PilotService(session, settings)


@router.get("/overview", response_model=PilotOverview)
async def get_pilot_overview(
    _context: AuthContext = Depends(require_admin),
    service: PilotService = Depends(get_pilot_service),
) -> PilotOverview:
    return await service.overview()


@router.get("/maintenance", response_model=PilotMaintenancePreview)
async def preview_pilot_maintenance(
    _context: AuthContext = Depends(require_admin),
    service: PilotService = Depends(get_pilot_service),
) -> PilotMaintenancePreview:
    return await service.maintenance_preview()


@router.post("/maintenance/cleanup", response_model=PilotCleanupResult)
async def cleanup_pilot_data(
    payload: PilotCleanupRequest,
    request: Request,
    context: AuthContext = Depends(require_admin_csrf),
    service: PilotService = Depends(get_pilot_service),
) -> PilotCleanupResult:
    del payload  # Literal 字段已完成不可误触的确认校验。
    return await service.cleanup(
        actor_user_id=context.user.user_id,
        request_id=request_id_context.get(),
        ip_address=request.client.host if request.client is not None else None,
    )
