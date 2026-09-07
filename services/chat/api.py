"""EvidenceRAG FastAPI 应用入口。"""

import asyncio
import json
from uuid import uuid4

from fastapi import (
    Depends,
    Query,
    Request,
)
from sse_starlette.sse import EventSourceResponse

from packages.platform.auth import (
    require_employee,
    require_employee_csrf,
)
from packages.platform.auth import AuthContext
from app.config import get_settings
from services.chat.models import AgentRun
from app.errors import ConversationNotFoundError, RunNotFoundError
from app.logging_config import (
    run_id_context,
)
from services.chat.conversations import ConversationStore, FeedbackStore
from services.chat.operations import router as operations_router
from services.chat.qa import KnowledgeQAService, get_knowledge_qa_service
from services.chat.workflow import RAGWorkflow, get_rag_workflow
from services.chat.runs import AgentRunStore
from app.schemas import (
    ConversationList,
    ConversationSummary,
    FeedbackCreate,
    FeedbackCreated,
    QueryDecision,
    RetrievalHitResult,
    RetrievalSearchCreate,
    RetrievalSearchResult,
    RunCreate,
    RunCreated,
    RunDetail,
    RunFailure,
    RunResult,
)



from packages.platform.application import create_app

settings = get_settings()
app = create_app("chat")
app.include_router(operations_router)

def get_workflow() -> RAGWorkflow:
    return get_rag_workflow()


def get_agent_run_store() -> AgentRunStore:
    return AgentRunStore()


def get_conversation_store() -> ConversationStore:
    return ConversationStore()


def get_feedback_store() -> FeedbackStore:
    return FeedbackStore()


def _query_decision(value) -> QueryDecision | None:
    if value is None:
        return None
    if isinstance(value, dict):
        payload = value
    else:
        payload = {
            "original_query": value.original_query,
            "standalone_question": value.standalone_question
            or value.original_query,
            "rewritten_query": value.rewritten_query,
            "applied": value.applied,
            "fallback": value.fallback,
            "reason": value.reason,
            "model": value.model,
            "usage": value.usage,
        }
    payload = {
        **payload,
        "standalone_question": payload.get("standalone_question")
        or payload.get("original_query", ""),
    }
    return QueryDecision.model_validate(payload)


def _run_detail(run: AgentRun) -> RunDetail:
    result = run.result or {}
    error = None
    if run.error_code and run.error_message:
        error = RunFailure(code=run.error_code, message=run.error_message)
    return RunDetail(
        run_id=run.run_id,
        conversation_id=run.conversation_id,
        question=run.question,
        status=run.status,
        current_node=run.current_node,
        query=result.get("query"),
        answer=result.get("answer"),
        citations=result.get("citations", []),
        model=result.get("model"),
        usage=result.get("usage"),
        error=error,
        events=run.events,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


@app.post("/api/v1/runs", response_model=RunCreated, status_code=202)
async def create_run(
    payload: RunCreate,
    context: AuthContext = Depends(require_employee_csrf),
    workflow: RAGWorkflow = Depends(get_workflow),
    store: AgentRunStore = Depends(get_agent_run_store),
) -> RunCreated:
    run_id = str(uuid4())
    run_id_context.set(run_id)
    run = await store.create(
        run_id,
        context.user.user_id,
        payload.question,
        payload.top_k,
        conversation_id=payload.conversation_id,
    )
    return RunCreated(
        run_id=run_id,
        conversation_id=run.conversation_id,
        status="queued",
        events_url=f"/api/v1/runs/{run_id}/events",
        result_url=f"/api/v1/runs/{run_id}",
    )


@app.get("/api/v1/runs/{run_id}", response_model=RunDetail)
async def get_run(
    run_id: str,
    context: AuthContext = Depends(require_employee),
    store: AgentRunStore = Depends(get_agent_run_store),
) -> RunDetail:
    run = await store.get_for_user(run_id, context.user.user_id)
    if run is None:
        raise RunNotFoundError()
    return _run_detail(run)


@app.get("/api/v1/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    context: AuthContext = Depends(require_employee),
    store: AgentRunStore = Depends(get_agent_run_store),
) -> EventSourceResponse:
    if await store.get_for_user(run_id, context.user.user_id) is None:
        raise RunNotFoundError()
    try:
        last_sequence = max(0, int(request.headers.get("Last-Event-ID", "0")))
    except ValueError:
        last_sequence = 0

    async def event_generator():
        nonlocal last_sequence
        while True:
            if await request.is_disconnected():
                break
            run = await store.get_for_user(run_id, context.user.user_id)
            if run is None:
                break
            for event in run.events:
                sequence = int(event["sequence"])
                if sequence <= last_sequence:
                    continue
                last_sequence = sequence
                yield {
                    "id": str(sequence),
                    "event": event["type"],
                    "data": json.dumps(event, ensure_ascii=False),
                }
            if run.status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.25)

    return EventSourceResponse(event_generator(), ping=15)


@app.post("/api/v1/knowledge/answers", response_model=RunResult, deprecated=True)
@app.post("/api/v1/answers", response_model=RunResult)
async def answer_question(
    payload: RunCreate,
    _context: AuthContext = Depends(require_employee_csrf),
    service: KnowledgeQAService = Depends(get_knowledge_qa_service),
) -> RunResult:
    run_id = str(uuid4())
    run_id_context.set(run_id)
    result = await service.answer(payload.question, top_k=payload.top_k)
    return RunResult(
        run_id=run_id,
        status="completed",
        answer=result.answer,
        query=_query_decision(result.query),
        citations=result.citations,
        model=result.model,
        usage=result.usage,
    )


@app.post("/api/v1/retrieval/search", response_model=RetrievalSearchResult)
async def search_knowledge(
    payload: RetrievalSearchCreate,
    _context: AuthContext = Depends(require_employee_csrf),
    service: KnowledgeQAService = Depends(get_knowledge_qa_service),
) -> RetrievalSearchResult:
    decision = await service.rewrite_query(payload.query) if payload.rewrite else None
    retrieval_query = decision.rewritten_query if decision else payload.query.strip()
    ranked = await service.retrieve(retrieval_query, top_k=payload.top_k)
    return RetrievalSearchResult(
        original_query=payload.query,
        retrieval_query=retrieval_query,
        query=_query_decision(decision),
        hits=[
            RetrievalHitResult(
                rank=index,
                chunk_id=item.hit.chunk_id,
                parent_chunk_id=item.hit.parent_chunk_id,
                document_id=item.hit.document_id,
                title=item.hit.title,
                matched_content=item.hit.content,
                parent_content=item.context_content,
                source=item.hit.source,
                version=item.hit.version,
                section_path=list(item.parent.section_path)
                if item.parent is not None
                else [],
                page_start=item.parent.page_start if item.parent is not None else None,
                page_end=item.parent.page_end if item.parent is not None else None,
                retrieval_score=item.hit.retrieval_score,
                rerank_score=item.rerank_score,
            )
            for index, item in enumerate(ranked, start=1)
        ],
    )


@app.get("/api/v1/conversations", response_model=ConversationList)
async def list_conversations(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: AuthContext = Depends(require_employee),
    store: ConversationStore = Depends(get_conversation_store),
) -> ConversationList:
    items = await store.list_recent(
        user_id=context.user.user_id,
        limit=limit,
        offset=offset,
    )
    return ConversationList(items=items, limit=limit, offset=offset)


@app.get(
    "/api/v1/conversations/{conversation_id}",
    response_model=ConversationSummary,
)
async def get_conversation(
    conversation_id: str,
    context: AuthContext = Depends(require_employee),
    store: ConversationStore = Depends(get_conversation_store),
) -> ConversationSummary:
    conversation = await store.get(
        conversation_id,
        user_id=context.user.user_id,
    )
    if conversation is None:
        raise ConversationNotFoundError()
    return ConversationSummary.model_validate(conversation)


@app.post("/api/v1/feedback", response_model=FeedbackCreated, status_code=201)
async def create_feedback(
    payload: FeedbackCreate,
    context: AuthContext = Depends(require_employee_csrf),
    store: FeedbackStore = Depends(get_feedback_store),
) -> FeedbackCreated:
    feedback_id = await store.create(
        user_id=context.user.user_id,
        run_id=payload.run_id,
        rating=payload.rating,
        comment=payload.comment,
        correction=payload.correction,
    )
    if feedback_id is None:
        raise RunNotFoundError()
    return FeedbackCreated(feedback_id=feedback_id)
