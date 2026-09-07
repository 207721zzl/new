"""RAG 领域模型与内部数据传输对象。"""

from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceBlock(BaseModel):
    """解析器输出的最小结构块，保留正文之外的定位信息。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1)
    block_type: str = Field(default="paragraph", min_length=1, max_length=64)
    section_path: list[str] = Field(default_factory=list)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    row_start: int | None = Field(default=None, ge=1)
    row_end: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ranges(self) -> "SourceBlock":
        if self.page_start and self.page_end and self.page_start > self.page_end:
            raise ValueError("page_start must not exceed page_end")
        if self.row_start and self.row_end and self.row_start > self.row_end:
            raise ValueError("row_start must not exceed row_end")
        return self


class KnowledgeDocument(BaseModel):
    """格式无关的知识文档；原项目的 JSON 字段仍可直接加载。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    document_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(default="")
    blocks: list[SourceBlock] = Field(default_factory=list, exclude=True)
    doc_type: str = Field(default="document", min_length=1, max_length=64)
    # 兼容原经营知识库；通用文档可为空。
    metric_name: str = Field(default="", max_length=128)
    source: str = Field(min_length=1, max_length=512)
    version: str = Field(default="1", min_length=1, max_length=32)
    updated_at: date
    original_filename: str | None = Field(default=None, max_length=255)
    mime_type: str | None = Field(default=None, max_length=128)
    content_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    language: str = Field(default="zh-CN", min_length=2, max_length=16)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_content_or_blocks(self) -> "KnowledgeDocument":
        if not self.content.strip() and not self.blocks:
            raise ValueError("document must contain content or blocks")
        if not self.content.strip():
            self.content = "\n\n".join(block.text for block in self.blocks)
        return self


class ParentKnowledgeChunk(BaseModel):
    """保存在 MySQL、用于生成阶段恢复完整上下文的父块。"""

    parent_chunk_id: str = Field(min_length=64, max_length=64)
    document_id: str
    title: str
    content: str
    parent_index: int = Field(ge=0)
    doc_type: str
    metric_name: str = ""
    source: str
    version: str
    updated_at: date
    section_path: list[str] = Field(default_factory=list)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    block_type: str = "mixed"
    language: str = "zh-CN"
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeChunk(BaseModel):
    """写入 Milvus、负责召回与重排的子块。"""

    chunk_id: str = Field(min_length=64, max_length=64)
    parent_chunk_id: str = Field(min_length=64, max_length=64)
    document_id: str
    title: str
    content: str
    parent_index: int = Field(ge=0)
    chunk_index: int = Field(ge=0)
    doc_type: str
    metric_name: str = ""
    source: str
    version: str
    updated_at: date
    section_path: list[str] = Field(default_factory=list)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    block_type: str = "mixed"
    language: str = "zh-CN"

    @property
    def embedding_text(self) -> str:
        """给 Dense 模型补充标题与章节，降低短子块的语义歧义。"""
        parts = [f"文档：{self.title}"]
        if self.section_path:
            parts.append(f"章节：{' / '.join(self.section_path)}")
        parts.append(self.content)
        return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """Milvus 混合检索返回的标准化子块候选。"""

    chunk_id: str
    parent_chunk_id: str
    document_id: str
    title: str
    content: str
    chunk_index: int
    doc_type: str
    metric_name: str
    source: str
    version: str
    updated_at: str
    retrieval_score: float


@dataclass(frozen=True, slots=True)
class ParentKnowledgeContext:
    """从 MySQL 状态真源读取的父块生成上下文。"""

    parent_chunk_id: str
    document_id: str
    title: str
    content: str
    parent_index: int
    source: str
    version: str
    updated_at: str
    section_path: tuple[str, ...] = ()
    page_start: int | None = None
    page_end: int | None = None
    block_type: str = "mixed"


@dataclass(frozen=True, slots=True)
class RankedKnowledge:
    """经交叉编码器重排后的知识候选。"""

    hit: RetrievalHit
    rerank_score: float
    parent: ParentKnowledgeContext | None = None

    @property
    def context_content(self) -> str:
        return self.parent.content if self.parent is not None else self.hit.content


@dataclass(frozen=True, slots=True)
class DeepSeekCompletion:
    answer: str
    model: str
    usage: dict[str, int] | None = None


class QueryRewriteResult(BaseModel):
    """模型返回的独立问题和面向混合检索的查询。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    standalone_question: str | None = Field(default=None, min_length=1, max_length=2_000)
    rewritten_query: str = Field(min_length=1, max_length=2_000)
    reason: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True, slots=True)
class QueryRewriteOutcome:
    original_query: str
    rewritten_query: str
    applied: bool
    fallback: bool
    reason: str
    standalone_question: str | None = None
    model: str | None = None
    usage: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeAnswer:
    answer: str
    citations: list[dict[str, Any]]
    model: str | None
    usage: dict[str, int] | None
    query: QueryRewriteOutcome | None = None
