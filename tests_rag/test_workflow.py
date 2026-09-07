import asyncio

from app.rag.models import KnowledgeAnswer, QueryRewriteOutcome
from app.rag.workflow import RAGWorkflow


class FakeService:
    async def rewrite_query(self, question, history):
        return QueryRewriteOutcome(
            original_query=question,
            standalone_question="完整问题",
            rewritten_query="完整问题 关键词",
            applied=True,
            fallback=False,
            reason="补充上下文",
        )

    async def retrieve(self, query, top_k=None):
        assert query == "完整问题 关键词"
        return []

    @staticmethod
    def insufficient_answer(query=None):
        return KnowledgeAnswer(
            answer="现有知识库资料不足以回答该问题。",
            citations=[],
            model=None,
            usage=None,
            query=query,
        )


def test_workflow_emits_rewrite_retrieve_and_safe_refusal():
    workflow = RAGWorkflow(FakeService())

    async def collect_updates():
        return [
            update
            async for update in workflow.astream(
                {"question": "那它呢？", "history_messages": [], "top_k": 5},
                stream_mode="updates",
            )
        ]

    updates = asyncio.run(collect_updates())
    assert [next(iter(update)) for update in updates] == [
        "rewrite_query",
        "retrieve_knowledge",
        "answer_without_knowledge",
    ]
    assert updates[-1]["answer_without_knowledge"]["citations"] == []
