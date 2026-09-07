"""RAG 服务编排、模型热身和推理排队超时测试。"""

import asyncio

import pytest

from app.config import Settings
from app.errors import LocalModelError, QueryRewriteError
from app.rag.models import (
    DeepSeekCompletion,
    ParentKnowledgeContext,
    QueryRewriteOutcome,
    RetrievalHit,
)
from app.rag.reranker import RerankResult
from app.rag.service import KnowledgeQAService


class FakeEmbedder:
    def encode(self, texts):
        assert texts == ["退款率是什么？"]
        return [[0.1, 0.2, 0.3]]


class FakeStore:
    def __init__(self, hits):
        self.hits = hits
        self.closed = False
        self.candidate_top_k = None

    def hybrid_search(self, query, dense_vector, candidate_top_k):
        assert query == "退款率是什么？"
        assert dense_vector == [0.1, 0.2, 0.3]
        self.candidate_top_k = candidate_top_k
        return self.hits

    def close(self):
        self.closed = True


class FakeReranker:
    def rerank(self, query, documents, top_k=None):
        assert top_k == 2
        assert len(documents) == 2
        return [RerankResult(content=documents[1], score=0.98, original_index=1)]


class FakeGenerator:
    def __init__(self):
        self.knowledge = None

    async def generate(self, question, knowledge):
        self.knowledge = knowledge
        return DeepSeekCompletion(
            answer="退款率按退款金额率计算。[1]",
            model="deepseek-v4-pro",
            usage={"total_tokens": 42},
        )


class FakeParentStore:
    async def get_many(self, parent_chunk_ids):
        return {
            parent_chunk_id: ParentKnowledgeContext(
                parent_chunk_id=parent_chunk_id,
                document_id="metric_refund_rate",
                title="退款率指标定义",
                content="退款率父块完整上下文，包含公式、适用范围和例外条件。",
                parent_index=0,
                source="指标手册",
                version="1.0",
                updated_at="2026-07-16",
            )
            for parent_chunk_id in parent_chunk_ids
        }


def make_hit(document_id: str, score: float) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=(document_id[0] * 64),
        parent_chunk_id=(document_id[-1] * 64),
        document_id=document_id,
        title=document_id,
        content=f"{document_id} content",
        chunk_index=0,
        doc_type="metric_definition",
        metric_name=document_id,
        source="指标手册",
        version="1.0",
        updated_at="2026-07-16",
        retrieval_score=score,
    )


def test_service_runs_hybrid_retrieval_reranker_and_generation():
    settings = Settings(
        _env_file=None,
        embedding_dimension=3,
        retrieval_top_k=1,
        hybrid_candidate_top_k=8,
    )
    store = FakeStore(
        [
            make_hit("metric_paid_gmv", 0.8),
            make_hit("metric_refund_rate", 0.7),
        ]
    )
    generator = FakeGenerator()
    service = KnowledgeQAService(
        settings=settings,
        embedder=FakeEmbedder(),
        reranker=FakeReranker(),
        generator=generator,
        store_factory=lambda: store,
        parent_store=FakeParentStore(),
    )

    result = asyncio.run(service.answer("退款率是什么？"))

    assert result.answer.endswith("[1]")
    assert result.citations[0]["document_id"] == "metric_refund_rate"
    assert result.citations[0]["retrieval_score"] == 0.7
    assert result.citations[0]["rerank_score"] == 0.98
    assert generator.knowledge[0].hit.document_id == "metric_refund_rate"
    assert generator.knowledge[0].context_content.startswith("退款率父块完整上下文")
    assert store.candidate_top_k == 8
    assert store.closed is True


class WarmupEmbedder:
    def __init__(self):
        self.called = False

    def encode(self, texts):
        self.called = True
        assert texts == ["经营指标口径是什么？"]
        return [[0.1, 0.2, 0.3]]


class WarmupReranker:
    def __init__(self):
        self.called = False

    def rerank(self, query, documents, top_k=None):
        self.called = True
        assert query == "经营指标口径是什么？"
        assert top_k == 1
        return [
            RerankResult(
                content=documents[0],
                score=0.9,
                original_index=0,
            )
        ]


def test_service_warmup_runs_both_local_models():
    settings = Settings(_env_file=None, embedding_dimension=3)
    embedder = WarmupEmbedder()
    reranker = WarmupReranker()
    service = KnowledgeQAService(
        settings=settings,
        embedder=embedder,
        reranker=reranker,
        generator=FakeGenerator(),
        store_factory=lambda: FakeStore([]),
    )

    elapsed_ms = service.warmup()

    assert elapsed_ms >= 0
    assert embedder.called is True
    assert reranker.called is True


def test_model_queue_timeout_is_reported_as_local_model_error():
    settings = Settings(
        _env_file=None,
        embedding_dimension=3,
        inference_queue_timeout_seconds=0.01,
    )
    service = KnowledgeQAService(
        settings=settings,
        embedder=WarmupEmbedder(),
        reranker=WarmupReranker(),
        generator=FakeGenerator(),
        store_factory=lambda: FakeStore([]),
    )
    service._model_lock.acquire()
    try:
        with pytest.raises(LocalModelError, match="embedding"):
            service._embed_query("blocked query")
    finally:
        service._model_lock.release()


class FailingQueryRewriter:
    async def rewrite(self, query):
        raise QueryRewriteError("temporary upstream failure")


def test_query_rewrite_failure_falls_back_to_original_query():
    service = KnowledgeQAService(
        settings=Settings(_env_file=None, embedding_dimension=3),
        embedder=WarmupEmbedder(),
        reranker=WarmupReranker(),
        generator=FakeGenerator(),
        store_factory=lambda: FakeStore([]),
        query_rewriter=FailingQueryRewriter(),
    )

    outcome = asyncio.run(service.rewrite_query("退款率是什么？"))

    assert outcome == QueryRewriteOutcome(
        original_query="退款率是什么？",
        rewritten_query="退款率是什么？",
        applied=False,
        fallback=True,
        reason="查询重写失败，已使用原查询",
    )
