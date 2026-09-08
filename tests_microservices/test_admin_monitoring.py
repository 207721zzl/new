from datetime import UTC, datetime

import pytest

from packages.platform.admin_monitoring import (
    AdminMonitoringModels,
    AdminMonitoringStore,
    TokenAlertPolicy,
)


@pytest.mark.asyncio
async def test_admin_monitoring_returns_feedback_context_and_token_alerts(databases):
    from services.chat.db import session_factory
    from services.chat.models import AgentRun, Conversation, Message, UserFeedback

    now = datetime.now(UTC).replace(tzinfo=None)
    async with session_factory() as session:
        session.add(Conversation(conversation_id="conversation-1", user_id="employee-1", title="退款率"))
        await session.flush()
        session.add(
            AgentRun(
                run_id="run-1",
                conversation_id="conversation-1",
                turn_index=1,
                question="退款率为什么上升？",
                status="completed",
                current_node="completed",
                result={
                    "answer": "原因主要集中在售后退款。",
                    "usage": {
                        "prompt_tokens": 600,
                        "completion_tokens": 200,
                        "total_tokens": 800,
                    },
                    "query": {
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 50,
                            "total_tokens": 150,
                        }
                    },
                },
                events=[],
                created_at=now,
                started_at=now,
                completed_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            Message(
                message_id="message-1",
                conversation_id="conversation-1",
                run_id="run-1",
                role="assistant",
                content="原因主要集中在售后退款，需要进一步按品类拆分。",
                created_at=now,
            )
        )
        session.add(
            UserFeedback(
                feedback_id="feedback-1",
                run_id="run-1",
                rating=-1,
                comment="缺少品类拆分。",
                correction="补充家电品类数据。",
                created_at=now,
            )
        )
        await session.commit()

    async with session_factory() as session:
        store = AdminMonitoringStore(
            session,
            models=AdminMonitoringModels(
                agent_run=AgentRun,
                conversation=Conversation,
                message=Message,
                feedback=UserFeedback,
            ),
            token_policy=TokenAlertPolicy(
                window_hours=24,
                window_limit=1_000,
                per_run_threshold=900,
                warning_ratio=0.8,
            ),
        )
        feedback = await store.list_feedback(
            window_hours=24,
            rating=-1,
            limit=50,
            offset=0,
        )
        tokens = await store.token_usage()

    assert feedback["negative"] == 1
    assert feedback["positive_rate"] == 0
    assert feedback["items"][0]["question"] == "退款率为什么上升？"
    assert feedback["items"][0]["answer_preview"].startswith("原因主要集中")
    assert feedback["items"][0]["correction"] == "补充家电品类数据。"
    assert tokens["summary"] == {
        "prompt_tokens": 700,
        "completion_tokens": 250,
        "total_tokens": 950,
        "run_count": 1,
        "average_tokens_per_run": 950,
    }
    assert tokens["status"] == "warning"
    assert {alert["kind"] for alert in tokens["alerts"]} == {
        "window_limit",
        "single_run",
    }
    assert tokens["recent_runs"][0]["total_tokens"] == 950


@pytest.mark.asyncio
async def test_feedback_rating_filter_does_not_change_window_summary(databases):
    from services.chat.db import session_factory
    from services.chat.models import AgentRun, Conversation, Message, UserFeedback

    async with session_factory() as session:
        store = AdminMonitoringStore(
            session,
            models=AdminMonitoringModels(
                agent_run=AgentRun,
                conversation=Conversation,
                message=Message,
                feedback=UserFeedback,
            ),
            token_policy=TokenAlertPolicy(24, 1_000, 500, 0.8),
        )
        result = await store.list_feedback(
            window_hours=24,
            rating=1,
            limit=50,
            offset=0,
        )

    assert result["items"] == []
    assert result["total"] == 0
    assert result["positive"] == 0
    assert result["negative"] == 0
