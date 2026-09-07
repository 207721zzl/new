"""独立 RAG HTTP API 使用的输入输出模型。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


RunStatus = Literal["queued", "running", "completed", "failed"]


class RunCreate(BaseModel):
    question: str = Field(min_length=1, max_length=2_000)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=36)
    top_k: int | None = Field(default=None, ge=1, le=20)


class KnowledgeCitation(BaseModel):
    index: int = Field(ge=1)
    chunk_id: str
    parent_chunk_id: str
    document_id: str
    title: str
    source: str
    version: str
    updated_at: str
    section_path: list[str] = Field(default_factory=list)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    retrieval_score: float
    rerank_score: float


class TokenUsage(BaseModel):
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class QueryDecision(BaseModel):
    original_query: str
    standalone_question: str
    rewritten_query: str
    applied: bool
    fallback: bool
    reason: str
    model: str | None = None
    usage: TokenUsage | None = None


class RunResult(BaseModel):
    run_id: str
    status: Literal["completed"]
    answer: str
    query: QueryDecision | None = None
    citations: list[KnowledgeCitation] = Field(default_factory=list)
    model: str | None = None
    usage: TokenUsage | None = None


class RunCreated(BaseModel):
    run_id: str
    conversation_id: str
    status: Literal["queued"]
    events_url: str
    result_url: str


class RunEvent(BaseModel):
    sequence: int = Field(ge=1)
    type: str
    node: str
    message: str
    status: RunStatus
    timestamp: datetime


class RunFailure(BaseModel):
    code: str
    message: str


class RunDetail(BaseModel):
    run_id: str
    conversation_id: str
    question: str
    status: RunStatus
    current_node: str
    query: QueryDecision | None = None
    answer: str | None = None
    citations: list[KnowledgeCitation] = Field(default_factory=list)
    model: str | None = None
    usage: TokenUsage | None = None
    error: RunFailure | None = None
    events: list[RunEvent] = Field(default_factory=list)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class RetrievalSearchCreate(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    top_k: int | None = Field(default=None, ge=1, le=20)
    rewrite: bool = True


class RetrievalHitResult(BaseModel):
    rank: int = Field(ge=1)
    chunk_id: str
    parent_chunk_id: str
    document_id: str
    title: str
    matched_content: str
    parent_content: str
    source: str
    version: str
    section_path: list[str] = Field(default_factory=list)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    retrieval_score: float
    rerank_score: float


class RetrievalSearchResult(BaseModel):
    original_query: str
    retrieval_query: str
    query: QueryDecision | None = None
    hits: list[RetrievalHitResult]


class ReadinessResult(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, bool]
    knowledge_chunks: int = Field(ge=0)


class ConversationMessage(BaseModel):
    message_id: str
    run_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class ConversationSummary(BaseModel):
    conversation_id: str
    run_id: str | None
    title: str
    messages: list[ConversationMessage]
    created_at: datetime
    updated_at: datetime


class ConversationList(BaseModel):
    items: list[ConversationSummary]
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class FeedbackCreate(BaseModel):
    run_id: str = Field(min_length=1, max_length=36)
    rating: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=2_000)
    correction: str | None = Field(default=None, max_length=5_000)


class FeedbackCreated(BaseModel):
    feedback_id: str
    status: Literal["recorded"] = "recorded"


class KnowledgeIndexCreate(BaseModel):
    source_file: str | None = Field(default=None, max_length=512)
    recreate: bool = False


class KnowledgeIndexCreated(BaseModel):
    job_id: str
    status: Literal["pending"] = "pending"
    status_url: str


IndexJobStatus = Literal["pending", "running", "completed", "failed"]


class KnowledgeIndexDetail(BaseModel):
    job_id: str
    status: IndexJobStatus
    source_file: str
    recreate: bool
    document_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class KnowledgeUploadItemCreated(BaseModel):
    job_id: str
    filename: str
    status_url: str


class KnowledgeUploadBatchCreated(BaseModel):
    batch_id: str
    status: Literal["pending"] = "pending"
    status_url: str
    items: list[KnowledgeUploadItemCreated]


KnowledgeUploadBatchStatus = Literal[
    "pending", "running", "completed", "partial_failed", "failed"
]


class KnowledgeUploadItemDetail(BaseModel):
    job_id: str
    filename: str
    status: IndexJobStatus
    file_size_bytes: int = Field(ge=0)
    document_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class KnowledgeUploadBatchDetail(BaseModel):
    batch_id: str
    status: KnowledgeUploadBatchStatus
    file_count: int = Field(ge=1)
    completed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    document_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    items: list[KnowledgeUploadItemDetail]


class KnowledgeDocumentSummary(BaseModel):
    document_id: str
    title: str
    doc_type: str
    source: str
    version: str
    original_filename: str | None = None
    mime_type: str | None = None
    language: str
    index_status: str
    parent_chunk_count: int = Field(ge=0)
    child_chunk_count: int = Field(ge=0)
    updated_at: datetime


class KnowledgeDocumentList(BaseModel):
    items: list[KnowledgeDocumentSummary]
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
