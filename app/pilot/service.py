"""单机试点的持久指标汇总和显式数据清理。"""

import math
import shutil
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.constants import (
    AUDIT_OUTCOME_SUCCESS,
    LEGACY_USER_ID,
    ROLE_ADMIN,
    ROLE_EMPLOYEE,
    USER_STATUS_ACTIVE,
    USER_STATUS_DISABLED,
    USER_STATUS_PENDING,
)
from app.config import PROJECT_ROOT, Settings
from app.db.models import (
    AdminAuditLog,
    AgentRun,
    AuthSession,
    Conversation,
    IndexingJob,
    KnowledgeChunkRecord,
    KnowledgeDocumentRecord,
    KnowledgeParentChunkRecord,
    ToolCall,
    User,
    UserFeedback,
)
from app.pilot.runtime import pilot_runtime_metrics
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
from app.rag.runtime import model_runtime_status


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return round(ordered[rank - 1], 2)


def _workload_summary(
    rows: list[tuple[str, datetime | None, datetime | None, datetime]],
    *,
    stale_before: datetime,
) -> PilotWorkloadSummary:
    counts = {status: 0 for status in ("queued", "running", "completed", "failed")}
    durations: list[float] = []
    stale = 0
    for status, started_at, completed_at, updated_at in rows:
        if status in counts:
            counts[status] += 1
        if status in {"queued", "running"} and updated_at < stale_before:
            stale += 1
        if started_at is not None and completed_at is not None:
            durations.append(max(0.0, (completed_at - started_at).total_seconds() * 1000))
    terminal = counts["completed"] + counts["failed"]
    return PilotWorkloadSummary(
        total=len(rows),
        **counts,
        stale=stale,
        success_rate=(round(counts["completed"] / terminal, 4) if terminal else None),
        average_duration_ms=(round(sum(durations) / len(durations), 2) if durations else None),
        p95_duration_ms=_nearest_rank(durations, 0.95),
    )


class PilotService:
    """从 MySQL 真源生成试点看板，并执行带审计的保留期清理。"""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def overview(self) -> PilotOverview:
        now = _utcnow()
        since = now - timedelta(hours=self.settings.pilot_metrics_window_hours)
        stale_before = now - timedelta(minutes=self.settings.pilot_stale_task_minutes)

        account_rows = (
            await self.session.execute(
                select(User.role, User.status, func.count())
                .where(User.user_id != LEGACY_USER_ID)
                .group_by(User.role, User.status)
            )
        ).all()
        account_counts = {(role, status): int(count) for role, status, count in account_rows}
        total_accounts = sum(account_counts.values())
        idle_deadline = now - timedelta(minutes=self.settings.auth_session_idle_minutes)
        active_sessions = int(
            await self.session.scalar(
                select(func.count())
                .select_from(AuthSession)
                .join(User, User.user_id == AuthSession.user_id)
                .where(
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                    AuthSession.last_seen_at > idle_deadline,
                    User.status == USER_STATUS_ACTIVE,
                )
            )
            or 0
        )

        documents, parents, children, source_bytes = await self._knowledge_counts()
        run_rows = (
            await self.session.execute(
                select(
                    AgentRun.status,
                    AgentRun.started_at,
                    AgentRun.completed_at,
                    AgentRun.updated_at,
                ).where(AgentRun.created_at >= since)
            )
        ).all()
        indexing_rows = (
            await self.session.execute(
                select(
                    IndexingJob.status,
                    IndexingJob.started_at,
                    IndexingJob.completed_at,
                    IndexingJob.updated_at,
                ).where(IndexingJob.created_at >= since)
            )
        ).all()
        feedback_rows = (
            await self.session.execute(
                select(UserFeedback.rating, func.count())
                .where(UserFeedback.created_at >= since)
                .group_by(UserFeedback.rating)
            )
        ).all()
        feedback_counts = {int(rating): int(count) for rating, count in feedback_rows}
        positive = feedback_counts.get(1, 0)
        negative = feedback_counts.get(-1, 0)
        token_summary = await self._token_usage(since)
        runtime = PilotRuntimeSummary.model_validate(pilot_runtime_metrics.snapshot())
        model_snapshot = model_runtime_status.snapshot()
        disk_free = shutil.disk_usage(PROJECT_ROOT).free

        runs = _workload_summary(list(run_rows), stale_before=stale_before)
        indexing = _workload_summary(list(indexing_rows), stale_before=stale_before)
        warnings: list[str] = []
        active_admins = account_counts.get((ROLE_ADMIN, USER_STATUS_ACTIVE), 0)
        pending = sum(
            count for (role, status), count in account_counts.items() if status == USER_STATUS_PENDING
        )
        disabled = sum(
            count for (role, status), count in account_counts.items() if status == USER_STATUS_DISABLED
        )
        if active_admins < 2:
            warnings.append("建议至少配置两名实名管理员，避免管理能力单点失效。")
        if pending:
            warnings.append(f"有 {pending} 个注册申请等待处理。")
        if runs.stale or indexing.stale:
            warnings.append("存在超过阈值仍未结束的任务，请检查服务日志。")
        if runs.failed or indexing.failed:
            warnings.append("统计窗口内存在失败任务，请确认原因后再扩大试点。")
        if documents == 0:
            warnings.append("知识库为空，问答验收暂不具备代表性。")
        if self.settings.app_environment == "production" and self.settings.auth_cookie_secure is not True:
            warnings.append("正式 HTTPS 入口应显式设置 AUTH_COOKIE_SECURE=true。")
        if self.settings.auth_registration_enabled:
            warnings.append("当前开放自助注册；封闭试点建议关闭并由管理员创建账号。")
        warnings.append("问题、最近对话和命中文档会发送至外部生成服务，请确认数据策略。")

        return PilotOverview(
            generated_at=now,
            window_hours=self.settings.pilot_metrics_window_hours,
            runtime=runtime,
            accounts=PilotAccountSummary(
                total=total_accounts,
                active_admins=active_admins,
                active_employees=account_counts.get((ROLE_EMPLOYEE, USER_STATUS_ACTIVE), 0),
                pending=pending,
                disabled=disabled,
                active_sessions=active_sessions,
            ),
            knowledge=PilotKnowledgeSummary(
                documents=documents,
                parent_chunks=parents,
                child_chunks=children,
                indexed_source_bytes=source_bytes,
            ),
            runs=runs,
            indexing=indexing,
            feedback=PilotFeedbackSummary(
                positive=positive,
                negative=negative,
                positive_rate=(round(positive / (positive + negative), 4) if positive + negative else None),
            ),
            tokens=token_summary,
            disk_free_bytes=disk_free,
            model_state=model_snapshot.state,
            warnings=warnings,
        )

    async def _knowledge_counts(self) -> tuple[int, int, int, int]:
        values = []
        for model in (
            KnowledgeDocumentRecord,
            KnowledgeParentChunkRecord,
            KnowledgeChunkRecord,
        ):
            values.append(
                int(await self.session.scalar(select(func.count()).select_from(model)) or 0)
            )
        source_bytes = int(
            await self.session.scalar(
                select(func.coalesce(func.sum(IndexingJob.file_size_bytes), 0)).where(
                    IndexingJob.status == "completed"
                )
            )
            or 0
        )
        return values[0], values[1], values[2], source_bytes

    async def _token_usage(self, since: datetime) -> PilotTokenSummary:
        results = await self.session.scalars(
            select(AgentRun.result).where(
                AgentRun.created_at >= since,
                AgentRun.status == "completed",
                AgentRun.result.is_not(None),
            )
        )
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for result in results:
            if not isinstance(result, dict):
                continue
            usages: list[Any] = [result.get("usage")]
            query = result.get("query")
            if isinstance(query, dict):
                usages.append(query.get("usage"))
            for usage in usages:
                if not isinstance(usage, dict):
                    continue
                for key in totals:
                    value = usage.get(key)
                    if isinstance(value, int) and value >= 0:
                        totals[key] += value
        return PilotTokenSummary(**totals)

    def _retention_cutoffs(self, now: datetime) -> tuple[datetime, datetime, datetime]:
        return (
            now - timedelta(days=self.settings.pilot_conversation_retention_days),
            now - timedelta(days=self.settings.pilot_session_retention_days),
            now - timedelta(days=self.settings.pilot_audit_retention_days),
        )

    async def maintenance_preview(self) -> PilotMaintenancePreview:
        now = _utcnow()
        conversation_before, session_before, audit_before = self._retention_cutoffs(now)
        conversation_count = int(
            await self.session.scalar(
                select(func.count()).select_from(Conversation).where(
                    Conversation.updated_at < conversation_before
                )
            )
            or 0
        )
        session_count = int(
            await self.session.scalar(
                select(func.count()).select_from(AuthSession).where(
                    or_(
                        AuthSession.expires_at < session_before,
                        and_(
                            AuthSession.revoked_at.is_not(None),
                            AuthSession.revoked_at < session_before,
                        ),
                    )
                )
            )
            or 0
        )
        audit_count = int(
            await self.session.scalar(
                select(func.count()).select_from(AdminAuditLog).where(
                    AdminAuditLog.created_at < audit_before
                )
            )
            or 0
        )
        return PilotMaintenancePreview(
            generated_at=now,
            conversation_retention_days=self.settings.pilot_conversation_retention_days,
            session_retention_days=self.settings.pilot_session_retention_days,
            audit_retention_days=self.settings.pilot_audit_retention_days,
            conversations_to_delete=conversation_count,
            sessions_to_delete=session_count,
            audit_logs_to_delete=audit_count,
        )

    async def cleanup(
        self,
        *,
        actor_user_id: str,
        request_id: str | None,
        ip_address: str | None,
    ) -> PilotCleanupResult:
        preview = await self.maintenance_preview()
        now = _utcnow()
        conversation_before, session_before, audit_before = self._retention_cutoffs(now)
        old_run_ids = select(AgentRun.run_id).join(Conversation).where(
            Conversation.updated_at < conversation_before
        )
        await self.session.execute(delete(ToolCall).where(ToolCall.run_id.in_(old_run_ids)))
        await self.session.execute(
            delete(Conversation).where(Conversation.updated_at < conversation_before)
        )
        await self.session.execute(
            delete(AuthSession).where(
                or_(
                    AuthSession.expires_at < session_before,
                    and_(
                        AuthSession.revoked_at.is_not(None),
                        AuthSession.revoked_at < session_before,
                    ),
                )
            )
        )
        await self.session.execute(
            delete(AdminAuditLog).where(AdminAuditLog.created_at < audit_before)
        )
        self.session.add(
            AdminAuditLog(
                audit_id=str(uuid4()),
                actor_user_id=actor_user_id,
                action="admin.pilot_retention_cleanup",
                target_type="pilot_data",
                target_id=None,
                outcome=AUDIT_OUTCOME_SUCCESS,
                request_id=request_id,
                ip_address=(ip_address or "")[:45] or None,
                details=preview.model_dump(mode="json"),
            )
        )
        await self.session.commit()
        return PilotCleanupResult(**preview.model_dump(), status="completed")
