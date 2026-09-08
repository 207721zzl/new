from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.base import Base
from app.db.models import AgentRun, Conversation, Message, User, UserFeedback
from packages.platform.admin_monitoring import (
    AdminMonitoringModels,
    AdminMonitoringStore,
    TokenAlertPolicy,
    extract_token_usage,
)


def test_token_usage_falls_back_to_prompt_plus_completion():
    assert extract_token_usage(
        {"usage": {"prompt_tokens": 12, "completion_tokens": 3}}
    ) == {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}


def test_default_token_alert_policy_is_configurable():
    settings = Settings(_env_file=None)

    assert settings.admin_token_alert_window_hours == 24
    assert settings.admin_token_alert_window_limit == 1_000_000
    assert settings.admin_token_alert_per_run_threshold == 10_000
    assert settings.admin_token_alert_warning_ratio == 0.8


@pytest.mark.asyncio
async def test_legacy_monitoring_includes_user_identity(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'monitoring.db'}")
    tables = [
        User.__table__,
        Conversation.__table__,
        AgentRun.__table__,
        Message.__table__,
        UserFeedback.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        async with sessions() as session:
            session.add(
                User(
                    user_id="employee-1",
                    username="li.ming",
                    normalized_username="li.ming",
                    display_name="李明",
                    password_hash="test-only",
                    role="employee",
                    status="active",
                    must_change_password=False,
                    failed_login_count=0,
                )
            )
            await session.flush()
            session.add(
                Conversation(
                    conversation_id="conversation-1",
                    user_id="employee-1",
                    title="销售口径",
                )
            )
            await session.flush()
            session.add(
                AgentRun(
                    run_id="run-1",
                    conversation_id="conversation-1",
                    turn_index=1,
                    question="销售口径是什么？",
                    status="completed",
                    current_node="completed",
                    result={"usage": {"total_tokens": 320}},
                    events=[],
                    created_at=now,
                    started_at=now,
                    completed_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add_all(
                [
                    Message(
                        message_id="message-1",
                        conversation_id="conversation-1",
                        run_id="run-1",
                        role="assistant",
                        content="以支付成功时间统计。",
                        created_at=now,
                    ),
                    UserFeedback(
                        feedback_id="feedback-1",
                        run_id="run-1",
                        rating=1,
                        comment="回答清楚。",
                        created_at=now,
                    ),
                ]
            )
            await session.commit()

        async with sessions() as session:
            store = AdminMonitoringStore(
                session,
                models=AdminMonitoringModels(
                    agent_run=AgentRun,
                    conversation=Conversation,
                    message=Message,
                    feedback=UserFeedback,
                    user=User,
                ),
                token_policy=TokenAlertPolicy(24, 1_000, 500, 0.8),
            )
            feedback = await store.list_feedback(
                window_hours=24,
                rating=None,
                limit=10,
                offset=0,
            )
            tokens = await store.token_usage()

        assert feedback["items"][0]["display_name"] == "李明"
        assert feedback["items"][0]["username"] == "li.ming"
        assert tokens["top_users"][0]["display_name"] == "李明"
    finally:
        await engine.dispose()
