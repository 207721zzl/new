"""Administrator monitoring service wired to the legacy application database."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AgentRun, Conversation, Message, User, UserFeedback
from packages.platform.admin_monitoring import (
    AdminMonitoringModels,
    AdminMonitoringStore,
    default_token_policy,
)


def create_admin_monitoring_store(session: AsyncSession) -> AdminMonitoringStore:
    return AdminMonitoringStore(
        session,
        models=AdminMonitoringModels(
            agent_run=AgentRun,
            conversation=Conversation,
            message=Message,
            feedback=UserFeedback,
            user=User,
        ),
        token_policy=default_token_policy(get_settings()),
    )
