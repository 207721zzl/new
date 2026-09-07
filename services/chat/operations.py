from datetime import timedelta
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select, func, delete
from sqlalchemy.exc import IntegrityError
from services.chat.db import session_factory
from services.chat.models import Conversation, AgentRun, UserFeedback, DurableTask
from app.config import get_settings
from packages.platform.tasks import now
from packages.contracts.metrics import workload

router = APIRouter(prefix="/internal/v1")


@router.post("/stats")
async def stats():
    config = get_settings()
    since = now() - timedelta(hours=config.pilot_metrics_window_hours)
    async with session_factory() as session:
        runs = list(
            await session.scalars(select(AgentRun).where(AgentRun.created_at >= since))
        )
        feedback = (
            await session.execute(
                select(UserFeedback.rating, func.count())
                .where(UserFeedback.created_at >= since)
                .group_by(UserFeedback.rating)
            )
        ).all()
    tokens = dict.fromkeys(["prompt_tokens", "completion_tokens", "total_tokens"], 0)
    for run in runs:
        result = run.result or {}
        for usage in [result.get("usage"), (result.get("query") or {}).get("usage")]:
            if isinstance(usage, dict):
                for key in tokens:
                    tokens[key] += max(0, int(usage.get(key) or 0))
    counts = dict(feedback)
    positive, negative = counts.get(1, 0), counts.get(-1, 0)
    return {
        "runs": workload(
            [(r.status, r.started_at, r.completed_at, r.updated_at) for r in runs],
            now() - timedelta(minutes=config.pilot_stale_task_minutes),
        ),
        "tokens": tokens,
        "feedback": {
            "positive": positive,
            "negative": negative,
            "positive_rate": positive / (positive + negative)
            if positive + negative
            else None,
        },
    }


class MaintenanceRequest(BaseModel):
    execute: bool = False
    operation_id: str = Field(min_length=36, max_length=36)
    actor_user_id: str


@router.post("/maintenance")
async def maintenance(payload: MaintenanceRequest):
    from services.chat.models import MaintenanceReceipt

    cutoff = now() - timedelta(days=get_settings().pilot_conversation_retention_days)
    async with session_factory() as session:
        if payload.execute:
            receipt = await session.get(MaintenanceReceipt, payload.operation_id)
            if receipt:
                return receipt.result
        active = select(AgentRun.conversation_id).where(
            AgentRun.status.in_(["queued", "running"])
        )
        rows = list(
            await session.scalars(
                select(Conversation)
                .where(
                    Conversation.updated_at < cutoff,
                    Conversation.conversation_id.not_in(active),
                )
                .with_for_update()
            )
        )
        ids = [row.conversation_id for row in rows]
        result = {"conversations_to_delete": len(ids)}
        if payload.execute:
            session.add(
                MaintenanceReceipt(operation_id=payload.operation_id, result=result)
            )
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                return (
                    await session.get(MaintenanceReceipt, payload.operation_id)
                ).result
            run_ids = list(
                await session.scalars(
                    select(AgentRun.run_id).where(AgentRun.conversation_id.in_(ids))
                )
            )
            await session.execute(
                delete(DurableTask).where(
                    DurableTask.resource_id.in_(run_ids),
                    DurableTask.status.in_(["completed", "failed"]),
                )
            )
            for row in rows:
                await session.delete(row)
            await session.commit()
        return result
