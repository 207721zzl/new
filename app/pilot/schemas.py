"""试点运行看板和数据维护 API 模型。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PilotRuntimeSummary(BaseModel):
    started_at: datetime
    uptime_seconds: float = Field(ge=0)
    requests_total: int = Field(ge=0)
    responses_4xx: int = Field(ge=0)
    responses_5xx: int = Field(ge=0)
    latency_sample_count: int = Field(ge=0)
    average_latency_ms: float | None = Field(default=None, ge=0)
    p95_latency_ms: float | None = Field(default=None, ge=0)


class PilotAccountSummary(BaseModel):
    total: int = Field(ge=0)
    active_admins: int = Field(ge=0)
    active_employees: int = Field(ge=0)
    pending: int = Field(ge=0)
    disabled: int = Field(ge=0)
    active_sessions: int = Field(ge=0)


class PilotKnowledgeSummary(BaseModel):
    documents: int = Field(ge=0)
    parent_chunks: int = Field(ge=0)
    child_chunks: int = Field(ge=0)
    indexed_source_bytes: int = Field(ge=0)


class PilotWorkloadSummary(BaseModel):
    total: int = Field(ge=0)
    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    stale: int = Field(ge=0)
    success_rate: float | None = Field(default=None, ge=0, le=1)
    average_duration_ms: float | None = Field(default=None, ge=0)
    p95_duration_ms: float | None = Field(default=None, ge=0)


class PilotFeedbackSummary(BaseModel):
    positive: int = Field(ge=0)
    negative: int = Field(ge=0)
    positive_rate: float | None = Field(default=None, ge=0, le=1)


class PilotTokenSummary(BaseModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class PilotOverview(BaseModel):
    generated_at: datetime
    window_hours: int = Field(ge=1)
    runtime: PilotRuntimeSummary
    accounts: PilotAccountSummary
    knowledge: PilotKnowledgeSummary
    runs: PilotWorkloadSummary
    indexing: PilotWorkloadSummary
    feedback: PilotFeedbackSummary
    tokens: PilotTokenSummary
    disk_free_bytes: int = Field(ge=0)
    model_state: str
    warnings: list[str]


class PilotMaintenancePreview(BaseModel):
    generated_at: datetime
    conversation_retention_days: int = Field(ge=1)
    session_retention_days: int = Field(ge=1)
    audit_retention_days: int = Field(ge=1)
    conversations_to_delete: int = Field(ge=0)
    sessions_to_delete: int = Field(ge=0)
    audit_logs_to_delete: int = Field(ge=0)


class PilotCleanupRequest(BaseModel):
    confirmation: Literal["PURGE_EXPIRED_PILOT_DATA"]


class PilotCleanupResult(PilotMaintenancePreview):
    status: Literal["completed"] = "completed"
