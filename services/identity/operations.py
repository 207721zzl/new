from datetime import timedelta
from uuid import uuid4
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select, func, delete, or_
from sqlalchemy.exc import IntegrityError
from services.identity.db import session_factory
from services.identity.models import User, AuthSession, AdminAuditLog
from packages.platform.tasks import now
from app.config import get_settings

router = APIRouter(prefix="/internal/v1")


class AuditEvent(BaseModel):
    audit_id: str = Field(min_length=36, max_length=36)
    actor_user_id: str
    action: str
    document_id: str
    details: dict


@router.post("/audit")
async def audit(event: AuditEvent):
    async with session_factory() as session:
        if await session.get(AdminAuditLog, event.audit_id) is None:
            session.add(AdminAuditLog(audit_id=event.audit_id, actor_user_id=event.actor_user_id,
                action=event.action, target_type="knowledge_document", target_id=event.document_id,
                outcome="success", details=event.details))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await session.get(AdminAuditLog, event.audit_id) is None:
                    raise
    return {"status": "recorded"}


@router.post("/stats")
async def stats():
    async with session_factory() as session:
        rows = (await session.execute(select(User.role, User.status, func.count()).group_by(User.role, User.status))).all()
        counts = {(role, status): count for role, status, count in rows}
        active_sessions = await session.scalar(select(func.count()).select_from(AuthSession).where(
            AuthSession.revoked_at.is_(None), AuthSession.expires_at > now(),
            AuthSession.last_seen_at > now()-timedelta(minutes=get_settings().auth_session_idle_minutes)))
    return {"accounts": {"total": sum(counts.values()), "active_admins": counts.get(("admin","active"),0),
        "active_employees": counts.get(("employee","active"),0), "pending": sum(v for (_,s),v in counts.items() if s=="pending"),
        "disabled": sum(v for (_,s),v in counts.items() if s=="disabled"), "active_sessions": active_sessions}}


class MaintenanceRequest(BaseModel):
    execute: bool = False
    operation_id: str = Field(min_length=36, max_length=36)
    actor_user_id: str


@router.post("/maintenance")
async def maintenance(payload: MaintenanceRequest):
    from services.identity.models import MaintenanceReceipt
    config = get_settings()
    async with session_factory() as session:
        if payload.execute:
            receipt = await session.get(MaintenanceReceipt, payload.operation_id)
            if receipt:
                return receipt.result
        cutoff = now()-timedelta(days=config.pilot_session_retention_days)
        session_filter = or_(AuthSession.expires_at < cutoff, AuthSession.revoked_at < cutoff)
        audit_filter = AdminAuditLog.created_at < now()-timedelta(days=config.pilot_audit_retention_days)
        result = {"sessions_to_delete": await session.scalar(select(func.count()).select_from(AuthSession).where(session_filter)),
                  "audit_logs_to_delete": await session.scalar(select(func.count()).select_from(AdminAuditLog).where(audit_filter))}
        if payload.execute:
            session.add(MaintenanceReceipt(operation_id=payload.operation_id, result=result))
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                return (await session.get(MaintenanceReceipt, payload.operation_id)).result
            await session.execute(delete(AuthSession).where(session_filter))
            await session.execute(delete(AdminAuditLog).where(audit_filter))
            session.add(AdminAuditLog(audit_id=str(uuid4()), actor_user_id=payload.actor_user_id,
                action="admin.pilot_retention_cleanup", target_type="identity_data", outcome="success", details=result))
            await session.commit()
    return result
