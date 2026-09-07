"""离线检索指标汇总和 CSV 展开逻辑测试。"""

from app.config import Settings
from app.rag.evaluation import EvaluationCase, RetrievalEvaluator
from app.rag.models import RetrievalHit
from app.rag.reranker import RerankResult
from scripts.evaluate_retrieval import (
    DEFAULT_DEVELOPMENT_CASES,
    csv_rows,
    describe_evaluation_context,
)


def make_hit(document_id: str, score: float) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=document_id[0] * 64,
        parent_chunk_id=document_id[-1] * 64,
        document_id=document_id,
        title=document_id,
        content=f"{document_id} content",
        chunk_index=0,
        doc_type="test",
        metric_name=document_id,
        source="test",
        version="1.0",
        updated_at="2026-07-16",
        retrieval_score=score,
    )


class FakeEmbedder:
    def encode(self, texts):
        return [[float(index), 0.0, 0.0] for index, _ in enumerate(texts)]


class FakeStore:
    def __init__(self):
        self.closed = False

    def hybrid_search(self, query, dense_vector, candidate_top_k):
        assert candidate_top_k == 3
        if query == "question one":
            return [make_hit("wrong_one", 0.9), make_hit("expected_one", 0.8)]
        if query == "question two":
            return [make_hit("expected_two", 0.9), make_hit("wrong_two", 0.8)]
        return [make_hit("unrelated", 0.4)]

    def close(self):
        self.closed = True


class FakeReranker:
    def rerank(self, query, documents, top_k=None):
        if query == "question one":
            return [
                RerankResult(
                    content=documents[1],
                    score=0.95,
                    original_index=1,
                ),
                RerankResult(
                    content=documents[0],
                    score=0.2,
                    original_index=0,
                ),
            ]
        if query == "question two":
            return [
                RerankResult(
                    content=documents[0],
                    score=0.9,
                    original_index=0,
                )
            ]
        return [
            RerankResult(
                content=documents[0],
                score=0.01,
                original_index=0,
            )
        ]


def test_evaluator_compares_hybrid_and_reranked_results():
    settings = Settings(
        _env_file=None,
        embedding_dimension=3,
        hybrid_candidate_top_k=3,
        retrieval_top_k=3,
        reranker_min_score=0.1,
    )
    store = FakeStore()
    evaluator = RetrievalEvaluator(
        settings=settings,
        embedder=FakeEmbedder(),
        reranker=FakeReranker(),
        store_factory=lambda: store,
    )
    cases = [
        EvaluationCase(
            id="one",
            question="question one",
            expected_document_ids=["expected_one"],
        ),
        EvaluationCase(
            id="two",
            question="question two",
            expected_document_ids=["expected_two"],
        ),
        EvaluationCase(
            id="negative",
            question="unknown question",
            expected_document_ids=[],
        ),
    ]

    report = evaluator.evaluate(cases)

    assert report["summary"]["hybrid"]["hit_rate_at_1"] == 0.5
    assert report["summary"]["reranked"]["hit_rate_at_1"] == 1.0
    assert report["summary"]["reranked"]["mrr"] == 1.0
    assert report["summary"]["out_of_knowledge_accuracy"] == 1.0
    assert report["summary"]["passed_at_3_rate"] == 1.0
    assert report["cases"][0]["reranked_hits"][0]["document_id"] == ("expected_one")
    assert report["cases"][2]["reranked_hits"] == []
    assert store.closed is True

    rows = csv_rows(report)
    assert rows[0]["expected_document_ids"] == "expected_one"
    assert rows[0]["reranked_document_ids"].startswith("expected_one")


def test_default_retrieval_cases_are_explicitly_non_blind():
    context = describe_evaluation_context(DEFAULT_DEVELOPMENT_CASES)

    assert context["evaluation_kind"] == "development_regression"
    assert context["is_blind"] is False
    assert context["corpus_and_cases_co_designed"] is True
    assert context["generalization_claim_allowed"] is False
