from services.chat.db import database as database, session_factory
from services.chat.models import DurableTask, Conversation
from services.chat.runs import AgentRunStore, execute_agent_run
from services.chat.workflow import get_rag_workflow
from packages.platform.tasks import TaskRunner


async def handle(task):
    store = AgentRunStore()
    run = await store.get(task.resource_id)
    if run is None or run.status in {"completed", "failed"}:
        return
    async with session_factory() as session:
        conversation = await session.get(Conversation, run.conversation_id)
        if conversation is None:
            return
        user_id = conversation.user_id
    await execute_agent_run(run_id=run.run_id, conversation_id=run.conversation_id,
                            user_id=user_id, question=run.question, top_k=run.top_k,
                            workflow=get_rag_workflow(), store=store)


async def failed(task):
    store = AgentRunStore()
    run = await store.get(task.resource_id)
    if run and run.status not in {"completed", "failed"}:
        await store.fail(task.resource_id, sequence=0, error_code="task_retry_exhausted",
                         error_message="问答服务多次执行失败，请稍后重新提交。")


def runner():
    return TaskRunner(DurableTask, session_factory, handle, failed)
