"""知识问答 RAG 主链路的应用服务。

服务依次完成查询向量化、Milvus 混合检索、相关性重排和 DeepSeek 生成，并将
各依赖错误转换为 API 可安全暴露的错误类型。
"""

import asyncio
from collections.abc import Callable
from functools import lru_cache
from threading import Lock
from time import perf_counter

from app.config import Settings, get_settings
from app.errors import (
    ConfigurationError,
    LocalModelError,
    QueryRewriteError,
    RetrievalError,
)
from app.logging_config import get_logger
from app.rag.deepseek import DeepSeekClient
from app.rag.embeddings import BgeM3Embedder, get_embedder
from app.rag.milvus_store import MilvusKnowledgeStore
from app.rag.models import (
    KnowledgeAnswer,
    QueryRewriteOutcome,
    RankedKnowledge,
    RetrievalHit,
)
from app.rag.parent_store import ParentChunkStore
from app.rag.query_rewriter import DeepSeekQueryRewriter
from app.rag.reranker import BgeReranker, get_reranker
from app.rag.runtime import model_runtime_status


logger = get_logger("rag.service")


class KnowledgeQAService:
    """编排一次完整的知识问答请求。"""

    def __init__(
        self,
        settings: Settings,
        embedder: BgeM3Embedder,
        reranker: BgeReranker,
        generator: DeepSeekClient,
        store_factory: Callable[[], MilvusKnowledgeStore],
        parent_store: ParentChunkStore | None = None,
        query_rewriter: DeepSeekQueryRewriter | None = None,
    ) -> None:
        """注入模型、生成客户端和存储工厂，便于测试与资源复用。"""
        self.settings = settings
        self.embedder = embedder
        self.reranker = reranker
        self.generator = generator
        self.store_factory = store_factory
        self.parent_store = parent_store or ParentChunkStore()
        self.query_rewriter = query_rewriter
        # 本地模型共享设备资源；串行化推理以控制 CPU/GPU 峰值占用。
        self._model_lock = Lock()

    def warmup(self) -> float:
        """使用最小样本预热 Embedding 与 Reranker，并返回耗时。"""
        started_at = perf_counter()
        acquired = self._model_lock.acquire(
            timeout=self.settings.inference_queue_timeout_seconds
        )
        if not acquired:
            raise LocalModelError("Model warmup queue timed out")
        try:
            vectors = self.embedder.encode(["这份文档主要介绍什么？"])
            if len(vectors) != 1:
                raise ValueError("embedder warmup did not return one vector")
            results = self.reranker.rerank(
                "这份文档主要介绍什么？",
                ["文档介绍了知识库的范围、使用方式和注意事项。"],
                top_k=1,
            )
            if len(results) != 1:
                raise ValueError("reranker warmup did not return one result")
        finally:
            self._model_lock.release()
        elapsed_ms = (perf_counter() - started_at) * 1000
        logger.info("local models warmed up duration_ms=%.2f", elapsed_ms)
        return elapsed_ms

    async def answer(
        self,
        question: str,
        top_k: int | None = None,
        history_messages: list[dict[str, str]] | None = None,
    ) -> KnowledgeAnswer:
        """依次执行多轮补全、查询重写、检索和答案生成。"""
        rewrite = await self.rewrite_query(question, history_messages)
        ranked = await self.retrieve(rewrite.rewritten_query, top_k=top_k)
        if not ranked:
            return self.insufficient_answer(query=rewrite)
        result = await self.generate_answer(
            rewrite.standalone_question or question,
            ranked,
        )
        return KnowledgeAnswer(
            answer=result.answer,
            citations=result.citations,
            model=result.model,
            usage=result.usage,
            query=rewrite,
        )

    async def rewrite_query(
        self,
        question: str,
        history_messages: list[dict[str, str]] | None = None,
    ) -> QueryRewriteOutcome:
        """生成检索专用查询；关闭或失败时安全回退到输入问题。"""
        normalized_question = question.strip()
        if not normalized_question:
            raise ValueError("question must not be empty")
        if not self.settings.query_rewrite_enabled:
            return QueryRewriteOutcome(
                original_query=normalized_question,
                standalone_question=normalized_question,
                rewritten_query=normalized_question,
                applied=False,
                fallback=False,
                reason="查询重写已关闭",
            )
        if self.query_rewriter is None:
            logger.warning("query rewriter is not configured; using original query")
            return QueryRewriteOutcome(
                original_query=normalized_question,
                standalone_question=normalized_question,
                rewritten_query=normalized_question,
                applied=False,
                fallback=True,
                reason="查询重写器未配置，已使用原查询",
            )
        try:
            try:
                return await self.query_rewriter.rewrite(
                    normalized_question,
                    history_messages,
                )
            except TypeError:
                # 兼容只接收 query 的测试替身和旧扩展。
                return await self.query_rewriter.rewrite(normalized_question)
        except (ConfigurationError, QueryRewriteError) as exc:
            # 查询重写只增强召回，不应因上游短暂异常阻断原有 RAG 链路。
            logger.warning(
                "query rewrite fell back error_type=%s",
                type(exc).__name__,
            )
            return QueryRewriteOutcome(
                original_query=normalized_question,
                standalone_question=normalized_question,
                rewritten_query=normalized_question,
                applied=False,
                fallback=True,
                reason="查询重写失败，已使用原查询",
            )

    async def retrieve(
        self,
        question: str,
        top_k: int | None = None,
    ) -> list[RankedKnowledge]:
        """执行向量化、混合检索、重排、阈值过滤和父块恢复。"""
        limit = top_k or self.settings.retrieval_top_k
        if limit < 1 or limit > 20:
            raise ValueError("top_k must be between 1 and 20")
        candidate_top_k = max(self.settings.hybrid_candidate_top_k, limit)

        logger.info(
            "knowledge QA started top_k=%s candidate_top_k=%s",
            limit,
            candidate_top_k,
        )
        dense_vector = await asyncio.to_thread(self._embed_query, question)
        hits = await asyncio.to_thread(
            self._retrieve,
            question,
            dense_vector,
            candidate_top_k,
        )
        # 没有候选时不调用大模型，直接返回可解释的知识不足结果。
        if not hits:
            logger.info("knowledge QA completed without retrieval hits")
            return []

        ranked = await asyncio.to_thread(self._rerank, question, hits, len(hits))
        # 相关性阈值是防止无关资料进入生成上下文的最后一道门槛。
        ranked = [
            item
            for item in ranked
            if item.rerank_score >= self.settings.reranker_min_score
        ]
        ranked = self._deduplicate_parents(ranked)[:limit]
        if not ranked:
            logger.info("knowledge QA completed without relevant reranked hits")
            return []
        return await self._expand_parent_context(ranked)

    async def generate_answer(
        self,
        question: str,
        ranked: list[RankedKnowledge],
    ) -> KnowledgeAnswer:
        """只使用已经通过证据门槛并恢复父块的上下文生成答案。"""
        if not ranked:
            return self.insufficient_answer()
        completion = await self.generator.generate(question, ranked)
        citations = [
            {
                "index": index,
                "chunk_id": item.hit.chunk_id,
                "parent_chunk_id": item.hit.parent_chunk_id,
                "document_id": item.hit.document_id,
                "title": item.hit.title,
                "source": item.hit.source,
                "version": item.hit.version,
                "updated_at": item.hit.updated_at,
                "section_path": list(item.parent.section_path)
                if item.parent is not None
                else [],
                "page_start": item.parent.page_start
                if item.parent is not None
                else None,
                "page_end": item.parent.page_end
                if item.parent is not None
                else None,
                "retrieval_score": item.hit.retrieval_score,
                "rerank_score": item.rerank_score,
            }
            for index, item in enumerate(ranked, start=1)
        ]
        logger.info("knowledge QA completed citations=%s", len(citations))
        return KnowledgeAnswer(
            answer=completion.answer,
            citations=citations,
            model=completion.model,
            usage=completion.usage,
        )

    @staticmethod
    def insufficient_answer(
        query: QueryRewriteOutcome | None = None,
    ) -> KnowledgeAnswer:
        """返回不调用生成模型的稳定知识不足结果。"""
        return KnowledgeAnswer(
            answer="现有知识库资料不足以回答该问题。",
            citations=[],
            model=None,
            usage=None,
            query=query,
        )

    def _embed_query(self, question: str) -> list[float]:
        """在线程锁保护下生成唯一查询向量。"""
        try:
            acquired = self._model_lock.acquire(
                timeout=self.settings.inference_queue_timeout_seconds
            )
            if not acquired:
                raise TimeoutError("Embedding inference queue timed out")
            try:
                vectors = self.embedder.encode([question])
            finally:
                self._model_lock.release()
            if len(vectors) != 1:
                raise ValueError("embedder did not return one query vector")
            return vectors[0]
        except Exception as exc:
            logger.exception("query embedding failed")
            raise LocalModelError("Query embedding failed") from exc

    def _retrieve(
        self,
        question: str,
        dense_vector: list[float],
        candidate_top_k: int,
    ) -> list[RetrievalHit]:
        """执行混合检索并确保短生命周期 Store 被关闭。"""
        store = None
        try:
            store = self.store_factory()
            return store.hybrid_search(
                query=question,
                dense_vector=dense_vector,
                candidate_top_k=candidate_top_k,
            )
        except Exception as exc:
            logger.exception("hybrid retrieval failed")
            raise RetrievalError() from exc
        finally:
            if store is not None:
                store.close()

    def _rerank(
        self,
        question: str,
        hits: list[RetrievalHit],
        limit: int,
    ) -> list[RankedKnowledge]:
        """重排检索候选，并恢复每个结果对应的原始元数据。"""
        documents = [f"{hit.title}\n{hit.content}" for hit in hits]
        try:
            acquired = self._model_lock.acquire(
                timeout=self.settings.inference_queue_timeout_seconds
            )
            if not acquired:
                raise TimeoutError("Reranker inference queue timed out")
            try:
                results = self.reranker.rerank(question, documents, top_k=limit)
            finally:
                self._model_lock.release()
        except Exception as exc:
            logger.exception("knowledge reranking failed")
            raise LocalModelError("Knowledge reranking failed") from exc
        return [
            RankedKnowledge(
                hit=hits[result.original_index],
                rerank_score=result.score,
            )
            for result in results
        ]

    @staticmethod
    def _deduplicate_parents(
        ranked: list[RankedKnowledge],
    ) -> list[RankedKnowledge]:
        """同一父块只保留分数最高的命中子块，避免重复生成上下文。"""
        selected: list[RankedKnowledge] = []
        seen: set[str] = set()
        for item in ranked:
            parent_chunk_id = item.hit.parent_chunk_id
            if parent_chunk_id in seen:
                continue
            seen.add(parent_chunk_id)
            selected.append(item)
        return selected

    async def _expand_parent_context(
        self,
        ranked: list[RankedKnowledge],
    ) -> list[RankedKnowledge]:
        """按子块的 parent_chunk_id 从 MySQL 批量恢复父块。"""
        parent_ids = [item.hit.parent_chunk_id for item in ranked]
        try:
            parents = await self.parent_store.get_many(parent_ids)
        except Exception as exc:
            logger.exception("parent chunk lookup failed")
            raise RetrievalError("Parent chunk lookup failed") from exc
        missing = [parent_id for parent_id in parent_ids if parent_id not in parents]
        if missing:
            logger.error("parent chunks missing count=%s", len(set(missing)))
            raise RetrievalError("Milvus child index is inconsistent with parent store")
        return [
            RankedKnowledge(
                hit=item.hit,
                rerank_score=item.rerank_score,
                parent=parents[item.hit.parent_chunk_id],
            )
            for item in ranked
        ]


@lru_cache(maxsize=1)
def get_knowledge_qa_service() -> KnowledgeQAService:
    """创建、预热并缓存进程级知识问答服务。"""
    settings = get_settings()
    model_runtime_status.mark_loading()
    try:
        service = KnowledgeQAService(
            settings=settings,
            embedder=get_embedder(),
            reranker=get_reranker(),
            generator=DeepSeekClient(settings),
            store_factory=lambda: MilvusKnowledgeStore(settings),
            parent_store=ParentChunkStore(),
            query_rewriter=DeepSeekQueryRewriter(settings),
        )
        if settings.model_warmup_enabled:
            service.warmup()
    except Exception as exc:
        model_runtime_status.mark_failed(exc)
        raise
    model_runtime_status.mark_ready()
    return service
