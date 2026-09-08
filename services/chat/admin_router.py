"""Administrator feedback and Token monitoring endpoints for the chat domain."""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.schemas import AdminFeedbackList, AdminTokenUsageReport
from app.config import get_settings
from packages.platform.admin_monitoring import (
    AdminMonitoringModels,
    AdminMonitoringStore,
    default_token_policy,
)
from packages.platform.auth import AuthContext, require_admin
from services.chat.db import get_admin_session
from services.chat.models import AgentRun, Conversation, Message, UserFeedback


router = APIRouter(prefix="/api/v1/admin", tags=["administration"])


def get_admin_monitoring_store(
    session: AsyncSession = Depends(get_admin_session),
) -> AdminMonitoringStore:
    return AdminMonitoringStore(
        session,
        models=AdminMonitoringModels(
            agent_run=AgentRun,
            conversation=Conversation,
            message=Message,
            feedback=UserFeedback,
        ),
        token_policy=default_token_policy(get_settings()),
    )


@router.get("/feedback", response_model=AdminFeedbackList)
async def list_feedback(
    window_hours: int = Query(default=24, ge=1, le=24 * 30),
    rating: Literal[-1, 1] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _context: AuthContext = Depends(require_admin),
    store: AdminMonitoringStore = Depends(get_admin_monitoring_store),
) -> AdminFeedbackList:
    return AdminFeedbackList.model_validate(
        await store.list_feedback(
            window_hours=window_hours,
            rating=rating,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/token-usage", response_model=AdminTokenUsageReport)
async def get_token_usage(
    _context: AuthContext = Depends(require_admin),
    store: AdminMonitoringStore = Depends(get_admin_monitoring_store),
) -> AdminTokenUsageReport:
    return AdminTokenUsageReport.model_validate(await store.token_usage())
