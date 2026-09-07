import asyncio

from app.config import Settings
from app.rag.models import DeepSeekCompletion, ParentKnowledgeContext, RetrievalHit
from app.rag.reranker import RerankResult
from app.rag.service import KnowledgeQAService


class FakeEmbedder:
    def encode(self, texts):
        assert texts == ["数据保留期限是什么？"]
        return [[0.1, 0.2, 0.3]]


class FakeReranker:
    def rerank(self, query, documents, top_k=None):
        assert query == "数据保留期限是什么？"
        assert top_k == 2
        return [RerankResult(content=documents[1], score=0.94, original_index=1)]


class FakeGenerator:
    async def generate(self, question, knowledge):
        assert question == "数据保留期限是什么？"
        assert knowledge[0].context_content.startswith("完整父块")
        return DeepSeekCompletion(answer="默认保留三十天。[1]", model="fake")


class FakeParentStore:
    async def get_many(self, parent_ids):
        return {
            parent_id: ParentKnowledgeContext(
                parent_chunk_id=parent_id,
                document_id="policy",
                title="数据政策",
                content="完整父块：数据默认保留三十天，法律要求除外。",
                parent_index=0,
                source="policy.pdf",
                version="1",
                updated_at="2026-08-12",
                section_path=("数据管理", "保留期限"),
                page_start=8,
                page_end=8,
            )
            for parent_id in parent_ids
        }


class FakeStore:
    def __init__(self):
        self.closed = False

    def hybrid_search(self, query, dense_vector, candidate_top_k):
        assert dense_vector == [0.1, 0.2, 0.3]
        assert candidate_top_k == 8
        return [
            make_hit("other", "a", "b", 0.8),
            make_hit("policy", "c", "d", 0.7),
        ]

    def close(self):
        self.closed = True


def make_hit(document_id, child_char, parent_char, score):
    return RetrievalHit(
        chunk_id=child_char * 64,
        parent_chunk_id=parent_char * 64,
        document_id=document_id,
        title=document_id,
        content=f"{document_id} 子块",
        chunk_index=0,
        doc_type="policy",
        metric_name="",
        source="policy.pdf",
        version="1",
        updated_at="2026-08-12",
        retrieval_score=score,
    )


def test_service_runs_hybrid_retrieval_rerank_parent_restore_and_generation():
    settings = Settings(
        _env_file=None,
        query_rewrite_enabled=False,
        embedding_dimension=3,
        retrieval_top_k=1,
        hybrid_candidate_top_k=8,
    )
    store = FakeStore()
    service = KnowledgeQAService(
        settings=settings,
        embedder=FakeEmbedder(),
        reranker=FakeReranker(),
        generator=FakeGenerator(),
        store_factory=lambda: store,
        parent_store=FakeParentStore(),
    )

    result = asyncio.run(service.answer("数据保留期限是什么？"))

    assert result.answer == "默认保留三十天。[1]"
    assert result.citations[0]["document_id"] == "policy"
    assert result.citations[0]["section_path"] == ["数据管理", "保留期限"]
    assert result.citations[0]["page_start"] == 8
    assert result.citations[0]["rerank_score"] == 0.94
    assert store.closed is True
