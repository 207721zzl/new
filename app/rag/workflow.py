"""纯 RAG 工作流：多轮补全、检索、证据门控和答案生成。"""

from functools import lru_cache
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from app.rag.service import KnowledgeQAService


class RAGWorkflow:
    """提供与后台运行器兼容的流式节点更新接口。"""

    def __init__(self, service: "KnowledgeQAService") -> None:
        self.service = service

    async def astream(
        self,
        state: dict[str, Any],
        *,
        stream_mode: str = "updates",
    ) -> AsyncIterator[dict[str, dict[str, Any]]]:
        if stream_mode != "updates":
            raise ValueError("RAGWorkflow only supports updates stream mode")

        rewrite = await self.service.rewrite_query(
            state["question"],
            state.get("history_messages") or [],
        )
        query_metadata = {
            "original_query": rewrite.original_query,
            "standalone_question": rewrite.standalone_question
            or rewrite.original_query,
            "rewritten_query": rewrite.rewritten_query,
            "applied": rewrite.applied,
            "fallback": rewrite.fallback,
            "reason": rewrite.reason,
            "model": rewrite.model,
            "usage": rewrite.usage,
        }
        yield {
            "rewrite_query": {
                "standalone_question": query_metadata["standalone_question"],
                "retrieval_query": rewrite.rewritten_query,
                "query": query_metadata,
            }
        }

        ranked = await self.service.retrieve(
            rewrite.rewritten_query,
            top_k=state.get("top_k"),
        )
        yield {
            "retrieve_knowledge": {
                "ranked_knowledge": ranked,
                "evidence_count": len(ranked),
            }
        }

        if not ranked:
            result = self.service.insufficient_answer(query=rewrite)
            yield {
                "answer_without_knowledge": {
                    "answer": result.answer,
                    "citations": [],
                    "model": None,
                    "usage": None,
                }
            }
            return

        result = await self.service.generate_answer(
            query_metadata["standalone_question"],
            ranked,
        )
        yield {
            "generate_answer": {
                "answer": result.answer,
                "citations": result.citations,
                "model": result.model,
                "usage": result.usage,
            }
        }


@lru_cache(maxsize=1)
def get_rag_workflow() -> RAGWorkflow:
    # 延迟加载外部模型与 Milvus 依赖，让解析、切块和工作流单测保持轻量。
    from app.rag.service import get_knowledge_qa_service

    return RAGWorkflow(get_knowledge_qa_service())
