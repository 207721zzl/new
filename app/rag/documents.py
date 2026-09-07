"""知识文档加载、结构感知父子切块和稳定 ID 生成。"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from app.rag.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    ParentKnowledgeChunk,
    SourceBlock,
)


document_list_adapter = TypeAdapter(list[KnowledgeDocument])


def load_documents(path: Path) -> list[KnowledgeDocument]:
    """读取统一 JSON 文档数组，并兼容原项目已有字段。"""
    with path.open(encoding="utf-8-sig") as file:
        raw_documents = json.load(file)
    return document_list_adapter.validate_python(raw_documents)


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """按字符窗口切分，并优先在段落或中文标点处断开。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be between zero and chunk_size")

    normalized = normalize_text(text)
    if not normalized:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        hard_end = min(start + chunk_size, len(normalized))
        end = hard_end
        if hard_end < len(normalized):
            search_start = start + int(chunk_size * 0.6)
            split_points = [
                normalized.rfind(separator, search_start, hard_end)
                for separator in ("\n\n", "。", "；", "！", "？", ". ", "; ")
            ]
            best_split = max(split_points)
            if best_split >= search_start:
                end = best_split + (2 if normalized[best_split : best_split + 2] in {"\n\n", ". ", "; "} else 1)

        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(normalized):
            break
        start = max(end - overlap, start + 1)
    return chunks


@dataclass(frozen=True, slots=True)
class ParentChildChunks:
    parents: list[ParentKnowledgeChunk]
    children: list[KnowledgeChunk]


@dataclass(frozen=True, slots=True)
class _ParentDraft:
    content: str
    section_path: list[str]
    page_start: int | None
    page_end: int | None
    block_type: str
    metadata: dict


def _content_blocks(document: KnowledgeDocument) -> list[SourceBlock]:
    """优先使用解析器结构；旧 JSON 则按段落派生结构块。"""
    if document.blocks:
        return [
            block.model_copy(update={"text": normalize_text(block.text)})
            for block in document.blocks
            if normalize_text(block.text)
        ]
    paragraphs = re.split(r"\n\s*\n", normalize_text(document.content))
    return [SourceBlock(text=item, block_type="paragraph") for item in paragraphs if item]


def _split_oversized_block(
    block: SourceBlock,
    *,
    hard_limit: int,
    overlap: int,
) -> list[SourceBlock]:
    if len(block.text) <= hard_limit:
        return [block]
    return [
        block.model_copy(update={"text": part})
        for part in split_text(block.text, hard_limit, min(overlap, hard_limit - 1))
    ]


def _same_structural_group(left: SourceBlock, right: SourceBlock) -> bool:
    """章节和有明确页码的 PDF 页面不跨界拼接。"""
    if left.section_path != right.section_path and (
        left.section_path or right.section_path
    ):
        return False
    if left.page_start is not None and right.page_start is not None:
        return left.page_start == right.page_start
    return True


def _render_parent(blocks: list[SourceBlock]) -> _ParentDraft:
    section_path = blocks[0].section_path
    body = "\n\n".join(block.text for block in blocks)
    if section_path:
        heading = "章节：" + " / ".join(section_path)
        if not body.startswith(heading):
            body = f"{heading}\n\n{body}"
    pages = [
        page
        for block in blocks
        for page in (block.page_start, block.page_end)
        if page is not None
    ]
    types = {block.block_type for block in blocks}
    metadata = {
        "row_start": next(
            (block.row_start for block in blocks if block.row_start is not None),
            None,
        ),
        "row_end": next(
            (block.row_end for block in reversed(blocks) if block.row_end is not None),
            None,
        ),
    }
    return _ParentDraft(
        content=normalize_text(body),
        section_path=list(section_path),
        page_start=min(pages) if pages else None,
        page_end=max(pages) if pages else None,
        block_type=next(iter(types)) if len(types) == 1 else "mixed",
        metadata={key: value for key, value in metadata.items() if value is not None},
    )


def _build_parent_drafts(
    document: KnowledgeDocument,
    *,
    target_size: int,
    parent_overlap: int,
) -> list[_ParentDraft]:
    """按结构边界组装父块，长度是约束而不是首要切分依据。"""
    hard_limit = max(target_size, int(target_size * 1.25))
    source_blocks = [
        split_block
        for block in _content_blocks(document)
        for split_block in _split_oversized_block(
            block,
            hard_limit=hard_limit,
            overlap=parent_overlap,
        )
    ]
    drafts: list[_ParentDraft] = []
    current: list[SourceBlock] = []
    current_length = 0

    for block in source_blocks:
        separator_size = 2 if current else 0
        crosses_structure = bool(current) and not _same_structural_group(
            current[-1], block
        )
        exceeds_hard_limit = (
            bool(current)
            and current_length + separator_size + len(block.text) > hard_limit
        )
        reached_target = current_length >= target_size
        if crosses_structure or exceeds_hard_limit or (reached_target and current):
            drafts.append(_render_parent(current))
            current = []
            current_length = 0
        current.append(block)
        current_length += (2 if current_length else 0) + len(block.text)

    if current:
        drafts.append(_render_parent(current))
    return drafts


def chunk_documents(
    documents: list[KnowledgeDocument],
    *,
    parent_chunk_size: int,
    parent_overlap: int,
    child_chunk_size: int,
    child_overlap: int,
) -> ParentChildChunks:
    """结构优先生成父块，再在父块内生成带重叠的检索子块。"""
    parents: list[ParentKnowledgeChunk] = []
    children: list[KnowledgeChunk] = []
    for document in documents:
        parent_drafts = _build_parent_drafts(
            document,
            target_size=parent_chunk_size,
            parent_overlap=parent_overlap,
        )
        document_child_index = 0
        for parent_index, draft in enumerate(parent_drafts):
            parent_identity = (
                f"{document.document_id}:{document.version}:parent:{parent_index}:"
                f"{draft.section_path}:{draft.page_start}:{draft.content}"
            )
            parent_chunk_id = hashlib.sha256(
                parent_identity.encode("utf-8")
            ).hexdigest()
            parents.append(
                ParentKnowledgeChunk(
                    parent_chunk_id=parent_chunk_id,
                    document_id=document.document_id,
                    title=document.title,
                    content=draft.content,
                    parent_index=parent_index,
                    doc_type=document.doc_type,
                    metric_name=document.metric_name,
                    source=document.source,
                    version=document.version,
                    updated_at=document.updated_at,
                    section_path=draft.section_path,
                    page_start=draft.page_start,
                    page_end=draft.page_end,
                    block_type=draft.block_type,
                    language=document.language,
                    metadata=draft.metadata,
                )
            )
            for child_index, child_content in enumerate(
                split_text(draft.content, child_chunk_size, child_overlap)
            ):
                child_identity = (
                    f"{parent_chunk_id}:child:{child_index}:{child_content}"
                )
                chunk_id = hashlib.sha256(
                    child_identity.encode("utf-8")
                ).hexdigest()
                children.append(
                    KnowledgeChunk(
                        chunk_id=chunk_id,
                        parent_chunk_id=parent_chunk_id,
                        document_id=document.document_id,
                        title=document.title,
                        content=child_content,
                        parent_index=parent_index,
                        chunk_index=document_child_index,
                        doc_type=document.doc_type,
                        metric_name=document.metric_name,
                        source=document.source,
                        version=document.version,
                        updated_at=document.updated_at,
                        section_path=draft.section_path,
                        page_start=draft.page_start,
                        page_end=draft.page_end,
                        block_type=draft.block_type,
                        language=document.language,
                    )
                )
                document_child_index += 1
    return ParentChildChunks(parents=parents, children=children)
