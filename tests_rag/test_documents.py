from datetime import date

from app.rag.documents import chunk_documents, split_text
from app.rag.models import KnowledgeDocument, SourceBlock


def test_split_text_prefers_sentence_boundaries_and_overlap():
    text = "第一段介绍系统。第二段解释检索。第三段说明引用。" * 8
    chunks = split_text(text, chunk_size=60, overlap=10)
    assert len(chunks) > 1
    assert all(chunk.strip() for chunk in chunks)
    assert all(len(chunk) <= 60 for chunk in chunks)


def test_parent_chunks_respect_section_boundary_and_children_point_back():
    document = KnowledgeDocument(
        document_id="guide",
        title="知识库指南",
        blocks=[
            SourceBlock(
                text="安装步骤。" * 20,
                section_path=["部署", "安装"],
                block_type="paragraph",
            ),
            SourceBlock(
                text="配置步骤。" * 20,
                section_path=["部署", "配置"],
                block_type="paragraph",
            ),
        ],
        source="guide.md",
        updated_at=date(2026, 8, 12),
    )
    result = chunk_documents(
        [document],
        parent_chunk_size=300,
        parent_overlap=0,
        child_chunk_size=100,
        child_overlap=20,
    )

    assert [parent.section_path for parent in result.parents] == [
        ["部署", "安装"],
        ["部署", "配置"],
    ]
    parent_ids = {parent.parent_chunk_id for parent in result.parents}
    assert result.children
    assert all(child.parent_chunk_id in parent_ids for child in result.children)
    assert all("章节：部署 /" in child.embedding_text for child in result.children)


def test_pdf_parent_does_not_cross_page_and_ids_are_stable():
    document = KnowledgeDocument(
        document_id="manual",
        title="手册",
        blocks=[
            SourceBlock(text="第一页内容。", page_start=1, page_end=1),
            SourceBlock(text="第二页内容。", page_start=2, page_end=2),
        ],
        source="manual.pdf",
        updated_at=date(2026, 8, 12),
    )
    arguments = dict(
        parent_chunk_size=200,
        parent_overlap=0,
        child_chunk_size=100,
        child_overlap=20,
    )
    first = chunk_documents([document], **arguments)
    second = chunk_documents([document], **arguments)

    assert [(item.page_start, item.page_end) for item in first.parents] == [(1, 1), (2, 2)]
    assert [item.parent_chunk_id for item in first.parents] == [
        item.parent_chunk_id for item in second.parents
    ]
    assert [item.chunk_id for item in first.children] == [item.chunk_id for item in second.children]
