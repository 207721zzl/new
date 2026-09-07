"""在单实例重启时终结已失去执行器的后台任务，避免永久卡在运行中。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.constants import AUDIT_OUTCOME_SUCCESS
from app.db.models import AdminAuditLog, AgentRun, Conversation, IndexingJob, Message
from app.logging_config import get_logger


logger = get_logger("pilot.recovery")


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def reconcile_interrupted_tasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    """把上个 API 进程遗留的非终态任务变为可解释的失败状态。"""
    now = _utcnow()
    run_count = 0
    indexing_count = 0
    async with session_factory() as session:
        run_rows = (
            await session.execute(
                select(AgentRun, Conversation)
                .join(
                    Conversation,
                    Conversation.conversation_id == AgentRun.conversation_id,
                )
                .where(AgentRun.status.in_(("queued", "running")))
                .with_for_update()
            )
        ).all()
        for run, conversation in run_rows:
            run_count += 1
            sequence = max(
                (int(item.get("sequence", 0)) for item in (run.events or [])),
                default=0,
            ) + 1
            message = "服务重启中断了本次问答，请重新提交问题。"
            run.status = "failed"
            run.current_node = "failed"
            run.error_code = "run_interrupted_by_restart"
            run.error_message = message
            run.completed_at = now
            run.updated_at = now
            run.events = [
                *(run.events or []),
                {
                    "sequence": sequence,
                    "type": "run.failed",
                    "node": "failed",
                    "message": message,
                    "status": "failed",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            ]
            existing_message = await session.scalar(
                select(Message.message_id).where(
                    Message.run_id == run.run_id,
                    Message.role == "assistant",
                )
            )
            if existing_message is None:
                session.add(
                    Message(
                        message_id=str(uuid4()),
                        conversation_id=conversation.conversation_id,
                        run_id=run.run_id,
                        role="assistant",
                        content=message,
                    )
                )
            conversation.updated_at = now

        jobs = list(
            await session.scalars(
                select(IndexingJob)
                .where(IndexingJob.status.in_(("pending", "running")))
                .with_for_update()
            )
        )
        for job in jobs:
            indexing_count += 1
            job.status = "failed"
            job.error_message = (
                "API 服务重启导致入库任务中断。源文件仍保留，请由管理员重新上传。"
            )
            job.completed_at = now
            job.updated_at = now

        if run_count or indexing_count:
            session.add(
                AdminAuditLog(
                    audit_id=str(uuid4()),
                    actor_user_id=None,
                    action="system.interrupted_tasks_reconciled",
                    target_type="background_tasks",
                    target_id=None,
                    outcome=AUDIT_OUTCOME_SUCCESS,
                    details={
                        "agent_runs_failed": run_count,
                        "indexing_jobs_failed": indexing_count,
                    },
                )
            )
        await session.commit()

    if run_count or indexing_count:
        logger.warning(
            "interrupted tasks reconciled agent_runs=%s indexing_jobs=%s",
            run_count,
            indexing_count,
        )
    return {"agent_runs": run_count, "indexing_jobs": indexing_count}
