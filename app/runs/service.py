"""后台执行 RAG 工作流，并把状态与事件持久化到 MySQL。"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.models import AgentRun
from app.db.repositories import AgentRunRepository, ConversationRepository
from app.db.session import get_admin_session_factory
from app.errors import AppError
from app.logging_config import get_logger, run_id_context
from app.schemas import RunResult


logger = get_logger("runs.service")

NODE_MESSAGES = {
    "rewrite_query": "已完成多轮问题补全与检索查询改写",
    "retrieve_knowledge": "已完成知识库混合检索与相关性重排",
    "answer_without_knowledge": "未发现达到证据门槛的知识，已安全拒答",
    "generate_answer": "已生成最终回答",
}


def _event(
    sequence: int,
    event_type: str,
    *,
    node: str,
    message: str,
    status: str,
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "type": event_type,
        "node": node,
        "message": message,
        "status": status,
        "timestamp": datetime.now(UTC).isoformat(),
    }


class AgentRunStore:
    """用短生命周期会话读写运行，避免 SSE 长连接占用数据库连接。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory or get_admin_session_factory()
        self.settings = settings or get_settings()

    async def create(
        self,
        run_id: str,
        user_id: str,
        question: str,
        top_k: int | None,
        conversation_id: str | None = None,
    ) -> AgentRun:
        async with self.session_factory() as session:
            return await ConversationRepository(session).create_turn(
                user_id=user_id,
                requested_conversation_id=conversation_id,
                run_id=run_id,
                message_id=str(uuid4()),
                question=question,
                top_k=top_k,
                initial_event=_event(
                    1,
                    "run.created",
                    node="queued",
                    message="任务已创建，等待执行",
                    status="queued",
                ),
            )

    async def get_context(
        self,
        conversation_id: str,
        *,
        user_id: str,
        exclude_run_id: str,
    ) -> list[dict[str, str]]:
        """返回受轮数和字符预算约束的最近已完成会话上下文。"""
        async with self.session_factory() as session:
            messages = await ConversationRepository(session).list_context_messages(
                conversation_id,
                user_id=user_id,
                exclude_run_id=exclude_run_id,
                limit=self.settings.conversation_recent_turns * 2,
            )

        remaining = self.settings.conversation_context_max_chars
        per_message_limit = max(250, remaining // 2)
        selected: list[dict[str, str]] = []
        for message in reversed(messages):
            content = message.content.strip()
            if not content or remaining <= 0:
                continue
            content = content[: min(remaining, per_message_limit)]
            selected.append({"role": message.role, "content": content})
            remaining -= len(content)
        selected.reverse()
        return selected

    async def get(self, run_id: str) -> AgentRun | None:
        async with self.session_factory() as session:
            return await AgentRunRepository(session).get(run_id)

    async def get_for_user(self, run_id: str, user_id: str) -> AgentRun | None:
        """只读取属于指定用户会话的运行。"""
        async with self.session_factory() as session:
            return await AgentRunRepository(session).get_for_user(run_id, user_id)

    async def mark_running(self, run_id: str) -> None:
        async with self.session_factory() as session:
            await AgentRunRepository(session).mark_running(
                run_id,
                event=_event(
                    2,
                    "workflow.started",
                    node="workflow",
                    message="RAG 工作流开始执行",
                    status="running",
                ),
            )

    async def record_node(
        self,
        run_id: str,
        *,
        sequence: int,
        node: str,
    ) -> None:
        async with self.session_factory() as session:
            await AgentRunRepository(session).record_node(
                run_id,
                node=node,
                intent="knowledge_qa",
                event=_event(
                    sequence,
                    "node.completed",
                    node=node,
                    message=NODE_MESSAGES.get(node, f"节点 {node} 已完成"),
                    status="running",
                ),
            )

    async def complete(
        self,
        run_id: str,
        *,
        sequence: int,
        result: dict[str, Any],
    ) -> None:
        async with self.session_factory() as session:
            await AgentRunRepository(session).complete(
                run_id,
                result=result,
                intent="knowledge_qa",
                event=_event(
                    sequence,
                    "run.completed",
                    node="completed",
                    message="知识问答完成",
                    status="completed",
                ),
                commit=False,
            )
            await ConversationRepository(session).add_assistant_message(
                message_id=str(uuid4()),
                run_id=run_id,
                content=str(result.get("answer") or ""),
            )

    async def fail(
        self,
        run_id: str,
        *,
        sequence: int,
        error_code: str,
        error_message: str,
    ) -> None:
        async with self.session_factory() as session:
            await AgentRunRepository(session).fail(
                run_id,
                error_code=error_code,
                error_message=error_message,
                event=_event(
                    sequence,
                    "run.failed",
                    node="failed",
                    message=error_message,
                    status="failed",
                ),
                commit=False,
            )
            await ConversationRepository(session).add_assistant_message(
                message_id=str(uuid4()),
                run_id=run_id,
                content=error_message,
            )


async def execute_agent_run(
    run_id: str,
    conversation_id: str,
    user_id: str,
    question: str,
    top_k: int | None,
    workflow: Any,
    store: AgentRunStore,
) -> None:
    """执行一次 RAG 运行，并在每个节点完成后发布可重放事件。"""
    token = run_id_context.set(run_id)
    sequence = 2
    state: dict[str, Any] = {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "question": question,
        "history_messages": [],
        "top_k": top_k,
    }
    try:
        if hasattr(store, "get_context"):
            state["history_messages"] = await store.get_context(
                conversation_id,
                user_id=user_id,
                exclude_run_id=run_id,
            )
        await store.mark_running(run_id)
        if hasattr(workflow, "astream"):
            async for update in workflow.astream(state, stream_mode="updates"):
                for node, patch in update.items():
                    if isinstance(patch, dict):
                        state.update(patch)
                    sequence += 1
                    await store.record_node(
                        run_id,
                        sequence=sequence,
                        node=node,
                    )
        else:
            state.update(await workflow.ainvoke(state))
            sequence += 1
            await store.record_node(
                run_id,
                sequence=sequence,
                node="workflow",
            )

        result = RunResult(
            run_id=run_id,
            status="completed",
            query=state.get("query"),
            answer=state.get("answer", ""),
            citations=state.get("citations", []),
            model=state.get("model"),
            usage=state.get("usage"),
        ).model_dump(mode="json")
        sequence += 1
        await store.complete(
            run_id,
            sequence=sequence,
            result=result,
        )
        logger.info("background RAG run completed citations=%s", len(result["citations"]))
    except Exception as exc:
        logger.exception("background run failed")
        if isinstance(exc, AppError):
            error_code = exc.code
            error_message = exc.public_message
        else:
            error_code = "agent_run_failed"
            error_message = "知识问答任务执行失败，请稍后重试。"
        await store.fail(
            run_id,
            sequence=sequence + 1,
            error_code=error_code,
            error_message=error_message,
        )
    finally:
        run_id_context.reset(token)
