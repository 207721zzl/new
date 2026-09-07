"""知识文件校验、文本切分和稳定 chunk_id 测试。"""

from datetime import date

import pytest

from app.config import PROJECT_ROOT
from app.rag.documents import chunk_documents, load_documents, split_text
from app.rag.models import KnowledgeDocument


def test_sample_documents_are_valid_and_chunk_ids_are_stable():
    documents = load_documents(PROJECT_ROOT / "data/knowledge/metrics.json")
    first = chunk_documents(
        documents,
        parent_chunk_size=1600,
        parent_overlap=0,
        child_chunk_size=400,
        child_overlap=80,
    )
    second = chunk_documents(
        documents,
        parent_chunk_size=1600,
        parent_overlap=0,
        child_chunk_size=400,
        child_overlap=80,
    )

    assert len(documents) == 7
    assert {document.document_id for document in documents} >= {
        "metric_traffic_visits",
        "analysis_campaign_effectiveness",
    }
    assert len(first.parents) >= len(documents)
    assert len(first.children) >= len(first.parents)
    assert [parent.parent_chunk_id for parent in first.parents] == [
        parent.parent_chunk_id for parent in second.parents
    ]
    assert [chunk.chunk_id for chunk in first.children] == [
        chunk.chunk_id for chunk in second.children
    ]
    assert len({chunk.chunk_id for chunk in first.children}) == len(first.children)
    parent_ids = {parent.parent_chunk_id for parent in first.parents}
    assert all(chunk.parent_chunk_id in parent_ids for chunk in first.children)
    assert all(len(parent.content) <= 1600 for parent in first.parents)
    assert all(len(chunk.content) <= 400 for chunk in first.children)


def test_split_text_rejects_invalid_overlap():
    with pytest.raises(ValueError, match="overlap"):
        split_text("测试文本", chunk_size=100, overlap=100)


def test_long_document_is_split_into_parent_contexts_and_smaller_children():
    document = KnowledgeDocument(
        document_id="long_manual",
        title="长文档",
        content="".join(
            f"第{index}条规则用于说明经营指标的适用范围和例外条件。"
            for index in range(120)
        ),
        doc_type="manual",
        metric_name="general",
        source="测试手册",
        version="1.0",
        updated_at=date(2026, 7, 21),
    )

    chunking = chunk_documents(
        [document],
        parent_chunk_size=800,
        parent_overlap=0,
        child_chunk_size=240,
        child_overlap=40,
    )

    assert len(chunking.parents) > 1
    assert len(chunking.children) > len(chunking.parents)
    children_by_parent = {
        parent.parent_chunk_id: [
            child
            for child in chunking.children
            if child.parent_chunk_id == parent.parent_chunk_id
        ]
        for parent in chunking.parents
    }
    assert all(children_by_parent.values())
    assert all(
        child.content in parent.content
        for parent in chunking.parents
        for child in children_by_parent[parent.parent_chunk_id]
    )
