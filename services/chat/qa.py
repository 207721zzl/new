from functools import lru_cache
from app.config import get_settings
from app.errors import QueryRewriteError, ConfigurationError
from app.logging_config import get_logger
from app.rag.deepseek import DeepSeekClient
from app.rag.query_rewriter import DeepSeekQueryRewriter
from app.rag.models import KnowledgeAnswer, QueryRewriteOutcome, RankedKnowledge
from packages.platform.client import ServiceClient
from packages.contracts.rag import decode_evidence

logger = get_logger("chat.qa")

class KnowledgeQAService:
    def __init__(self):
        self.settings = get_settings()
        self.generator = DeepSeekClient(self.settings)
        self.query_rewriter = DeepSeekQueryRewriter(self.settings)
        self.knowledge = ServiceClient("knowledge")

    async def retrieve(self, question, top_k=None):
        data = await self.knowledge.post("/internal/v1/search", {"query": question, "top_k": top_k or self.settings.retrieval_top_k})
        return decode_evidence(data["evidence"])

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


@lru_cache(maxsize=1)
def get_knowledge_qa_service():
    return KnowledgeQAService()
