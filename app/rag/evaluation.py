"""混合检索与重排链路的离线评测模型和指标计算。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from statistics import fmean
from time import perf_counter

from pydantic import BaseModel, Field, TypeAdapter

from app.config import Settings
from app.logging_config import get_logger
from app.rag.embeddings import BgeM3Embedder
from app.rag.milvus_store import MilvusKnowledgeStore
from app.rag.models import RetrievalHit
from app.rag.reranker import BgeReranker


logger = get_logger("rag.evaluation")


class EvaluationCase(BaseModel):
    """一条带期望文档集合的检索评测问题。"""

    id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=2000)
    expected_document_ids: list[str]
    purpose: str = ""


class EvaluationHit(BaseModel):
    """用于报告展示的精简检索结果。"""

    rank: int = Field(ge=1)
    chunk_id: str
    document_id: str
    title: str
    retrieval_score: float
    rerank_score: float | None = None


class CaseEvaluation(BaseModel):
    """单条问题在混合检索和重排两个阶段的完整评测结果。"""

    id: str
    question: str
    purpose: str
    expected_document_ids: list[str]
    hybrid_hits: list[EvaluationHit]
    reranked_hits: list[EvaluationHit]
    matched_expected_ids: list[str]
    reciprocal_rank: float
    passed_at_3: bool
    latency_ms: dict[str, float]


evaluation_case_adapter = TypeAdapter(list[EvaluationCase])


def load_evaluation_cases(path: Path) -> list[EvaluationCase]:
    """读取并校验 JSON 格式的评测集。"""
    return evaluation_case_adapter.validate_json(path.read_text(encoding="utf-8"))


def ranked_document_ids(hits: Sequence[EvaluationHit]) -> list[str]:
    """按首次出现顺序提取去重后的文档 ID。"""
    document_ids: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.document_id not in seen:
            seen.add(hit.document_id)
            document_ids.append(hit.document_id)
    return document_ids


def recall_at_k(actual: Sequence[str], expected: Sequence[str], k: int) -> float:
    """计算前 K 个结果覆盖期望文档的比例。"""
    expected_set = set(expected)
    if not expected_set:
        return 0.0
    return len(set(actual[:k]) & expected_set) / len(expected_set)


def hit_at_k(actual: Sequence[str], expected: Sequence[str], k: int) -> float:
    """判断前 K 个结果中是否至少命中一个期望文档。"""
    expected_set = set(expected)
    if not expected_set:
        return 0.0
    return float(bool(set(actual[:k]) & expected_set))


def reciprocal_rank(actual: Sequence[str], expected: Sequence[str]) -> float:
    """计算首个相关文档排名的倒数。"""
    expected_set = set(expected)
    for rank, document_id in enumerate(actual, start=1):
        if document_id in expected_set:
            return 1.0 / rank
    return 0.0


def percentile(values: Sequence[float], value: int) -> float:
    """使用 nearest-rank 方法计算延迟百分位数。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, ceil((value / 100) * len(ordered)) - 1)
    return ordered[index]


class RetrievalEvaluator:
    """对真实 Embedding、Milvus 和 Reranker 运行离线检索评测。"""

    def __init__(
        self,
        settings: Settings,
        embedder: BgeM3Embedder,
        reranker: BgeReranker,
        store_factory: Callable[[], MilvusKnowledgeStore],
    ) -> None:
        """注入评测依赖；Store 工厂确保连接可在评测结束后释放。"""
        self.settings = settings
        self.embedder = embedder
        self.reranker = reranker
        self.store_factory = store_factory

    def evaluate(
        self,
        cases: list[EvaluationCase],
        candidate_top_k: int | None = None,
        rerank_top_k: int | None = None,
    ) -> dict:
        """批量评测所有用例并返回可序列化的汇总报告。"""
        if not cases:
            raise ValueError("evaluation cases must not be empty")
        candidate_limit = candidate_top_k or self.settings.hybrid_candidate_top_k
        rerank_limit = rerank_top_k or self.settings.retrieval_top_k
        if candidate_limit < 1 or candidate_limit > 200:
            raise ValueError("candidate_top_k must be between 1 and 200")
        if rerank_limit < 1 or rerank_limit > candidate_limit:
            raise ValueError("rerank_top_k must be between 1 and candidate_top_k")

        logger.info(
            "retrieval evaluation started cases=%s candidate_top_k=%s rerank_top_k=%s",
            len(cases),
            candidate_limit,
            rerank_limit,
        )
        # 批量编码所有问题，以贴近索引/评测任务的实际吞吐方式。
        embedding_started = perf_counter()
        vectors = self.embedder.encode([case.question for case in cases])
        embedding_total_ms = (perf_counter() - embedding_started) * 1000
        if len(vectors) != len(cases):
            raise ValueError(
                "Embedding count mismatch: "
                f"expected {len(cases)}, received {len(vectors)}"
            )
        embedding_per_case_ms = embedding_total_ms / len(cases)

        results: list[CaseEvaluation] = []
        store = self.store_factory()
        try:
            for index, (case, vector) in enumerate(
                zip(cases, vectors, strict=True),
                start=1,
            ):
                logger.info(
                    "evaluating case current=%s total=%s case_id=%s",
                    index,
                    len(cases),
                    case.id,
                )
                results.append(
                    self._evaluate_case(
                        case=case,
                        vector=vector,
                        store=store,
                        candidate_top_k=candidate_limit,
                        rerank_top_k=rerank_limit,
                        embedding_ms=embedding_per_case_ms,
                    )
                )
        finally:
            store.close()

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "config": {
                "collection": self.settings.milvus_collection,
                "embedding_model": self.settings.embedding_model,
                "reranker_model": self.settings.reranker_model,
                "candidate_top_k": candidate_limit,
                "rerank_top_k": rerank_limit,
                "reranker_min_score": self.settings.reranker_min_score,
            },
            "summary": self._summarize(results, embedding_total_ms),
            "cases": [result.model_dump(mode="json") for result in results],
        }
        logger.info("retrieval evaluation completed cases=%s", len(cases))
        return report

    def _evaluate_case(
        self,
        case: EvaluationCase,
        vector: list[float],
        store: MilvusKnowledgeStore,
        candidate_top_k: int,
        rerank_top_k: int,
        embedding_ms: float,
    ) -> CaseEvaluation:
        """评测单条问题并分别记录检索、重排和总延迟。"""
        retrieval_started = perf_counter()
        retrieved = store.hybrid_search(
            query=case.question,
            dense_vector=vector,
            candidate_top_k=candidate_top_k,
        )
        retrieval_ms = (perf_counter() - retrieval_started) * 1000
        hybrid_hits = self._hybrid_hits(retrieved)

        rerank_started = perf_counter()
        documents = [f"{hit.title}\n{hit.content}" for hit in retrieved]
        rerank_results = self.reranker.rerank(
            case.question,
            documents,
            top_k=min(rerank_top_k, len(documents)),
        )
        reranked_hits = [
            EvaluationHit(
                rank=rank,
                chunk_id=retrieved[result.original_index].chunk_id,
                document_id=retrieved[result.original_index].document_id,
                title=retrieved[result.original_index].title,
                retrieval_score=retrieved[result.original_index].retrieval_score,
                rerank_score=result.score,
            )
            for rank, result in enumerate(rerank_results, start=1)
            if result.score >= self.settings.reranker_min_score
        ]
        rerank_ms = (perf_counter() - rerank_started) * 1000

        document_ids = ranked_document_ids(reranked_hits)
        expected = case.expected_document_ids
        matched = [item for item in expected if item in set(document_ids)]
        passed_at_3 = (
            bool(set(document_ids[:3]) & set(expected))
            if expected
            else not document_ids
        )
        return CaseEvaluation(
            id=case.id,
            question=case.question,
            purpose=case.purpose,
            expected_document_ids=expected,
            hybrid_hits=hybrid_hits,
            reranked_hits=reranked_hits,
            matched_expected_ids=matched,
            reciprocal_rank=reciprocal_rank(document_ids, expected),
            passed_at_3=passed_at_3,
            latency_ms={
                "embedding": round(embedding_ms, 3),
                "hybrid_search": round(retrieval_ms, 3),
                "rerank": round(rerank_ms, 3),
                "total": round(embedding_ms + retrieval_ms + rerank_ms, 3),
            },
        )

    @staticmethod
    def _hybrid_hits(retrieved: list[RetrievalHit]) -> list[EvaluationHit]:
        """把内部检索对象转换为报告使用的排名结构。"""
        return [
            EvaluationHit(
                rank=rank,
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                title=hit.title,
                retrieval_score=hit.retrieval_score,
            )
            for rank, hit in enumerate(retrieved, start=1)
        ]

    @staticmethod
    def _summarize(
        results: list[CaseEvaluation],
        embedding_total_ms: float,
    ) -> dict:
        """聚合 Recall、Hit Rate、MRR、负例准确率和延迟分位数。"""
        positive = [item for item in results if item.expected_document_ids]
        negative = [item for item in results if not item.expected_document_ids]

        def stage_metrics(stage: str) -> dict:
            document_lists = [
                ranked_document_ids(getattr(item, stage)) for item in positive
            ]
            metrics: dict[str, float | int] = {"case_count": len(positive)}
            for k in (1, 3, 5):
                metrics[f"recall_at_{k}"] = round(
                    fmean(
                        recall_at_k(actual, item.expected_document_ids, k)
                        for actual, item in zip(
                            document_lists,
                            positive,
                            strict=True,
                        )
                    )
                    if positive
                    else 0.0,
                    4,
                )
                metrics[f"hit_rate_at_{k}"] = round(
                    fmean(
                        hit_at_k(actual, item.expected_document_ids, k)
                        for actual, item in zip(
                            document_lists,
                            positive,
                            strict=True,
                        )
                    )
                    if positive
                    else 0.0,
                    4,
                )
            metrics["mrr"] = round(
                fmean(
                    reciprocal_rank(actual, item.expected_document_ids)
                    for actual, item in zip(
                        document_lists,
                        positive,
                        strict=True,
                    )
                )
                if positive
                else 0.0,
                4,
            )
            return metrics

        total_latencies = [item.latency_ms["total"] for item in results]
        search_latencies = [item.latency_ms["hybrid_search"] for item in results]
        rerank_latencies = [item.latency_ms["rerank"] for item in results]
        negative_correct = sum(not item.reranked_hits for item in negative)
        return {
            "case_count": len(results),
            "positive_case_count": len(positive),
            "negative_case_count": len(negative),
            "hybrid": stage_metrics("hybrid_hits"),
            "reranked": stage_metrics("reranked_hits"),
            "out_of_knowledge_accuracy": round(
                negative_correct / len(negative) if negative else 0.0,
                4,
            ),
            "passed_at_3_count": sum(item.passed_at_3 for item in results),
            "passed_at_3_rate": round(
                sum(item.passed_at_3 for item in results) / len(results),
                4,
            ),
            "latency_ms": {
                "embedding_total": round(embedding_total_ms, 3),
                "hybrid_search_average": round(fmean(search_latencies), 3),
                "rerank_average": round(fmean(rerank_latencies), 3),
                "total_average": round(fmean(total_latencies), 3),
                "total_p50": round(percentile(total_latencies, 50), 3),
                "total_p95": round(percentile(total_latencies, 95), 3),
            },
        }
