"""Shared read models for the administrator feedback and token dashboards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession


TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _token_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def extract_token_usage(result: Any) -> dict[str, int]:
    """Return the answer and rewrite token usage stored on one completed run."""
    totals = dict.fromkeys(TOKEN_KEYS, 0)
    if not isinstance(result, dict):
        return totals

    query = result.get("query")
    usages = [result.get("usage")]
    if isinstance(query, dict):
        usages.append(query.get("usage"))

    for usage in usages:
        if not isinstance(usage, dict):
            continue
        prompt = _token_count(usage.get("prompt_tokens"))
        completion = _token_count(usage.get("completion_tokens"))
        total = _token_count(usage.get("total_tokens"))
        if total == 0:
            total = prompt + completion
        totals["prompt_tokens"] += prompt
        totals["completion_tokens"] += completion
        totals["total_tokens"] += total
    return totals


def _preview(value: Any, limit: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}…"


@dataclass(frozen=True, slots=True)
class AdminMonitoringModels:
    agent_run: Any
    conversation: Any
    message: Any
    feedback: Any
    user: Any | None = None


@dataclass(frozen=True, slots=True)
class TokenAlertPolicy:
    window_hours: int
    window_limit: int
    per_run_threshold: int
    warning_ratio: float


class AdminMonitoringStore:
    """Build privacy-aware administrator read models from the chat database."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        models: AdminMonitoringModels,
        token_policy: TokenAlertPolicy,
    ) -> None:
        self.session = session
        self.models = models
        self.token_policy = token_policy

    def _identity_columns(self) -> tuple[Any, Any]:
        user = self.models.user
        if user is None:
            return literal(None).label("username"), literal(None).label("display_name")
        return user.username, user.display_name

    async def list_feedback(
        self,
        *,
        window_hours: int,
        rating: int | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        feedback = self.models.feedback
        run = self.models.agent_run
        conversation = self.models.conversation
        message = self.models.message
        user = self.models.user
        since = _utcnow() - timedelta(hours=window_hours)

        base_conditions = [feedback.created_at >= since]
        counts = (
            await self.session.execute(
                select(feedback.rating, func.count())
                .where(*base_conditions)
                .group_by(feedback.rating)
            )
        ).all()
        rating_counts = {int(value): int(count) for value, count in counts}
        positive = rating_counts.get(1, 0)
        negative = rating_counts.get(-1, 0)

        item_conditions = list(base_conditions)
        if rating is not None:
            item_conditions.append(feedback.rating == rating)
        total = int(
            await self.session.scalar(
                select(func.count()).select_from(feedback).where(*item_conditions)
            )
            or 0
        )

        answer = (
            select(message.content)
            .where(message.run_id == run.run_id, message.role == "assistant")
            .order_by(message.created_at.desc())
            .limit(1)
            .correlate(run)
            .scalar_subquery()
        )
        username, display_name = self._identity_columns()
        statement = (
            select(feedback, run, conversation, username, display_name, answer.label("answer"))
            .join(run, run.run_id == feedback.run_id)
            .join(conversation, conversation.conversation_id == run.conversation_id)
        )
        if user is not None:
            statement = statement.outerjoin(user, user.user_id == conversation.user_id)
        rows = (
            await self.session.execute(
                statement.where(*item_conditions)
                .order_by(feedback.created_at.desc(), feedback.feedback_id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()

        items = []
        for item, item_run, item_conversation, item_username, item_display_name, item_answer in rows:
            items.append(
                {
                    "feedback_id": item.feedback_id,
                    "run_id": item.run_id,
                    "conversation_id": item_conversation.conversation_id,
                    "user_id": item_conversation.user_id,
                    "username": item_username,
                    "display_name": item_display_name,
                    "rating": item.rating,
                    "question": _preview(item_run.question),
                    "answer_preview": _preview(item_answer),
                    "comment": item.comment,
                    "correction": item.correction,
                    "created_at": item.created_at,
                }
            )

        return {
            "generated_at": _utcnow(),
            "window_hours": window_hours,
            "positive": positive,
            "negative": negative,
            "positive_rate": positive / (positive + negative) if positive + negative else None,
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def token_usage(self) -> dict[str, Any]:
        run = self.models.agent_run
        conversation = self.models.conversation
        user = self.models.user
        policy = self.token_policy
        now = _utcnow()
        since = now - timedelta(hours=policy.window_hours)
        username, display_name = self._identity_columns()
        statement = select(
            run.run_id,
            run.question,
            run.result,
            run.created_at,
            conversation.user_id,
            username,
            display_name,
        ).join(conversation, conversation.conversation_id == run.conversation_id)
        if user is not None:
            statement = statement.outerjoin(user, user.user_id == conversation.user_id)
        rows = (
            await self.session.execute(
                statement.where(
                    run.created_at >= since,
                    run.status == "completed",
                    run.result.is_not(None),
                ).order_by(run.created_at.desc())
            )
        ).all()

        totals = dict.fromkeys(TOKEN_KEYS, 0)
        run_items: list[dict[str, Any]] = []
        user_totals: dict[str, dict[str, Any]] = {}
        bucket_count = min(12, max(1, policy.window_hours))
        bucket_seconds = policy.window_hours * 3600 / bucket_count
        buckets = [
            {
                "started_at": since + timedelta(seconds=bucket_seconds * index),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
            for index in range(bucket_count)
        ]

        for run_id, question, result, created_at, user_id, item_username, item_display_name in rows:
            usage = extract_token_usage(result)
            for key in TOKEN_KEYS:
                totals[key] += usage[key]
            elapsed = max(0.0, (created_at - since).total_seconds())
            bucket_index = min(bucket_count - 1, int(elapsed / bucket_seconds))
            for key in TOKEN_KEYS:
                buckets[bucket_index][key] += usage[key]

            identity = item_display_name or item_username or user_id
            user_entry = user_totals.setdefault(
                user_id,
                {
                    "user_id": user_id,
                    "username": item_username,
                    "display_name": item_display_name,
                    "run_count": 0,
                    "total_tokens": 0,
                },
            )
            user_entry["run_count"] += 1
            user_entry["total_tokens"] += usage["total_tokens"]
            run_items.append(
                {
                    "run_id": run_id,
                    "user_id": user_id,
                    "username": item_username,
                    "display_name": item_display_name,
                    "identity": identity,
                    "question": _preview(question, 240) or "—",
                    **usage,
                    "created_at": created_at,
                }
            )

        usage_ratio = totals["total_tokens"] / policy.window_limit
        if usage_ratio >= 1:
            budget_status = "critical"
        elif usage_ratio >= policy.warning_ratio:
            budget_status = "warning"
        else:
            budget_status = "normal"

        alerts: list[dict[str, Any]] = []
        if budget_status != "normal":
            percent = round(usage_ratio * 100, 1)
            alerts.append(
                {
                    "kind": "window_limit",
                    "level": budget_status,
                    "message": f"最近 {policy.window_hours} 小时 Token 用量已达到窗口上限的 {percent}%",
                    "run_id": None,
                    "value": totals["total_tokens"],
                    "threshold": policy.window_limit,
                    "created_at": now,
                }
            )
        for item in run_items:
            if item["total_tokens"] < policy.per_run_threshold:
                continue
            level = "critical" if item["total_tokens"] >= policy.per_run_threshold * 2 else "warning"
            alerts.append(
                {
                    "kind": "single_run",
                    "level": level,
                    "message": f"{item['identity']} 的单次问答消耗 {item['total_tokens']:,} Token",
                    "run_id": item["run_id"],
                    "value": item["total_tokens"],
                    "threshold": policy.per_run_threshold,
                    "created_at": item["created_at"],
                }
            )
            if len(alerts) >= 10:
                break

        severity = {"normal": 0, "warning": 1, "critical": 2}
        status = budget_status
        for alert in alerts:
            if severity[alert["level"]] > severity[status]:
                status = alert["level"]

        top_users = sorted(
            user_totals.values(),
            key=lambda item: (item["total_tokens"], item["run_count"]),
            reverse=True,
        )[:8]
        run_count = len(run_items)
        return {
            "generated_at": now,
            "window_hours": policy.window_hours,
            "status": status,
            "summary": {
                **totals,
                "run_count": run_count,
                "average_tokens_per_run": round(totals["total_tokens"] / run_count)
                if run_count
                else 0,
            },
            "budget": {
                "window_limit": policy.window_limit,
                "per_run_threshold": policy.per_run_threshold,
                "warning_ratio": policy.warning_ratio,
                "usage_ratio": round(usage_ratio, 4),
                "remaining_tokens": max(0, policy.window_limit - totals["total_tokens"]),
                "status": budget_status,
            },
            "alerts": alerts,
            "series": buckets,
            "top_users": top_users,
            "recent_runs": run_items[:30],
        }


def default_token_policy(settings: Any) -> TokenAlertPolicy:
    return TokenAlertPolicy(
        window_hours=settings.admin_token_alert_window_hours,
        window_limit=settings.admin_token_alert_window_limit,
        per_run_threshold=settings.admin_token_alert_per_run_threshold,
        warning_ratio=settings.admin_token_alert_warning_ratio,
    )
