"""管理员后台 HTTP 请求与响应模型。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from app.auth.schemas import UserRole, UserStatus
from app.auth.security import (
    MAX_PASSWORD_LENGTH,
    validate_display_name,
    validate_password,
    validate_username,
)


class AdminUserSummary(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: UserRole
    status: UserStatus
    must_change_password: bool
    failed_login_count: int = Field(ge=0)
    locked_until: datetime | None = None
    last_login_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AdminUserList(BaseModel):
    items: list[AdminUserSummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    password: SecretStr
    role: UserRole = "employee"
    status: UserStatus = "active"
    must_change_password: bool = True

    @field_validator("username")
    @classmethod
    def validate_login_name(cls, value: str) -> str:
        return validate_username(value)

    @field_validator("display_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return validate_display_name(value)

    @field_validator("password")
    @classmethod
    def validate_temporary_password(cls, value: SecretStr) -> SecretStr:
        validate_password(value.get_secret_value())
        return value


class AdminUserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    role: UserRole | None = None
    status: UserStatus | None = None

    @field_validator("display_name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return validate_display_name(value) if value is not None else None

    @model_validator(mode="after")
    def require_a_change(self):
        if self.display_name is None and self.role is None and self.status is None:
            raise ValueError("at least one user field must be provided")
        return self


class AdminPasswordResetRequest(BaseModel):
    temporary_password: SecretStr

    @field_validator("temporary_password")
    @classmethod
    def validate_temporary_password(cls, value: SecretStr) -> SecretStr:
        password = value.get_secret_value()
        if len(password) > MAX_PASSWORD_LENGTH:
            raise ValueError("password is too long")
        validate_password(password)
        return value


class AdminUserResult(BaseModel):
    user: AdminUserSummary


class AdminAuditSummary(BaseModel):
    audit_id: str
    actor_user_id: str | None
    actor_username: str | None
    action: str
    target_type: str
    target_id: str | None
    outcome: Literal["success", "failure", "denied"]
    details: dict
    created_at: datetime


class AdminAuditList(BaseModel):
    items: list[AdminAuditSummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class KnowledgeDocumentDeleted(BaseModel):
    document_id: str
    title: str
    parent_chunk_count: int = Field(ge=0)
    child_chunk_count: int = Field(ge=0)
    status: Literal["deleted"] = "deleted"


class AdminFeedbackSummary(BaseModel):
    feedback_id: str
    run_id: str
    conversation_id: str
    user_id: str
    username: str | None = None
    display_name: str | None = None
    rating: Literal[-1, 1]
    question: str | None = None
    answer_preview: str | None = None
    comment: str | None = None
    correction: str | None = None
    created_at: datetime


class AdminFeedbackList(BaseModel):
    generated_at: datetime
    window_hours: int = Field(ge=1)
    positive: int = Field(ge=0)
    negative: int = Field(ge=0)
    positive_rate: float | None = Field(default=None, ge=0, le=1)
    items: list[AdminFeedbackSummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


TokenAlertLevel = Literal["normal", "warning", "critical"]


class AdminTokenSummary(BaseModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    run_count: int = Field(ge=0)
    average_tokens_per_run: int = Field(ge=0)


class AdminTokenBudget(BaseModel):
    window_limit: int = Field(ge=1)
    per_run_threshold: int = Field(ge=1)
    warning_ratio: float = Field(ge=0, lt=1)
    usage_ratio: float = Field(ge=0)
    remaining_tokens: int = Field(ge=0)
    status: TokenAlertLevel


class AdminTokenAlert(BaseModel):
    kind: Literal["window_limit", "single_run"]
    level: Literal["warning", "critical"]
    message: str
    run_id: str | None = None
    value: int = Field(ge=0)
    threshold: int = Field(ge=1)
    created_at: datetime


class AdminTokenSeriesPoint(BaseModel):
    started_at: datetime
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class AdminTokenUserSummary(BaseModel):
    user_id: str
    username: str | None = None
    display_name: str | None = None
    run_count: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class AdminTokenRunSummary(BaseModel):
    run_id: str
    user_id: str
    username: str | None = None
    display_name: str | None = None
    identity: str
    question: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    created_at: datetime


class AdminTokenUsageReport(BaseModel):
    generated_at: datetime
    window_hours: int = Field(ge=1)
    status: TokenAlertLevel
    summary: AdminTokenSummary
    budget: AdminTokenBudget
    alerts: list[AdminTokenAlert]
    series: list[AdminTokenSeriesPoint]
    top_users: list[AdminTokenUserSummary]
    recent_runs: list[AdminTokenRunSummary]
