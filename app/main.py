"""EvidenceRAG FastAPI 应用入口。"""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from app.admin.router import router as admin_router
from app.auth.dependencies import (
    get_optional_auth_context,
    require_admin,
    require_admin_csrf,
    require_employee,
    require_employee_csrf,
)
from app.auth.constants import ROLE_ADMIN
from app.auth.security import hash_security_token
from app.auth.router import router as auth_router
from app.auth.service import AuthContext
from app.config import PROJECT_ROOT, get_settings
from app.db.health import check_mysql_connection
from app.db.models import AgentRun
from app.db.session import dispose_database_engines, get_admin_session_factory
from app.errors import AppError, ConversationNotFoundError, RunNotFoundError
from app.logging_config import (
    configure_logging,
    get_logger,
    request_id_context,
    run_id_context,
)
from app.operations.service import (
    ConversationStore,
    FeedbackStore,
    IndexingJobStore,
    execute_indexing_batch,
    execute_indexing_job,
)
from app.operations.uploads import discard_staged_batch, stage_knowledge_uploads
from app.pilot.recovery import reconcile_interrupted_tasks
from app.pilot.rate_limit import pilot_rate_limiter
from app.pilot.router import router as pilot_router
from app.pilot.runtime import pilot_runtime_metrics
from app.rag.milvus_store import MilvusKnowledgeStore
from app.rag.runtime import model_runtime_status
from app.rag.service import KnowledgeQAService, get_knowledge_qa_service
from app.rag.workflow import RAGWorkflow, get_rag_workflow
from app.runs.service import AgentRunStore, execute_agent_run
from app.schemas import (
    ConversationList,
    ConversationSummary,
    FeedbackCreate,
    FeedbackCreated,
    KnowledgeIndexCreate,
    KnowledgeIndexCreated,
    KnowledgeIndexDetail,
    KnowledgeDocumentList,
    KnowledgeUploadBatchCreated,
    KnowledgeUploadBatchDetail,
    KnowledgeUploadItemCreated,
    KnowledgeUploadItemDetail,
    QueryDecision,
    ReadinessResult,
    RetrievalHitResult,
    RetrievalSearchCreate,
    RetrievalSearchResult,
    RunCreate,
    RunCreated,
    RunDetail,
    RunFailure,
    RunResult,
)


configure_logging()
settings = get_settings()
logger = get_logger("api")


def _pilot_client_key(request: Request) -> str:
    session_token = request.cookies.get(settings.auth_session_cookie_name)
    if session_token:
        return f"session:{hash_security_token(session_token)[:24]}"
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get(
        "x-forwarded-for",
        "",
    ).split(",", maxsplit=1)[0].strip()
    peer = request.client.host if request.client is not None else "unknown"
    return f"ip:{forwarded or peer}"


def _pilot_rate_policy(request: Request) -> tuple[str, int, int] | None:
    if not settings.pilot_rate_limit_enabled or request.method != "POST":
        return None
    path = request.url.path
    if path == "/api/v1/auth/login":
        return "login", settings.pilot_login_requests_per_5_minutes, 300
    if path == "/api/v1/auth/register":
        return "registration", settings.pilot_registration_requests_per_hour, 3600
    if path in {
        "/api/v1/runs",
        "/api/v1/answers",
        "/api/v1/knowledge/answers",
        "/api/v1/retrieval/search",
        "/api/v1/feedback",
    }:
        return "query", settings.pilot_query_requests_per_minute, 60
    if path == "/api/v1/knowledge/uploads":
        return "upload", settings.pilot_upload_requests_per_hour, 3600
    return None


def _apply_security_headers(request: Request, response: Response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'",
    )
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded_proto.split(",", maxsplit=1)[0].strip() or request.url.scheme
    if scheme.casefold() == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    """按配置预加载本地 Embedding 与 Reranker。"""
    logger.info("application starting environment=%s", settings.app_environment)
    if settings.pilot_reconcile_interrupted_tasks:
        try:
            await reconcile_interrupted_tasks(get_admin_session_factory())
        except Exception:
            logger.exception("interrupted task reconciliation failed")
    if settings.model_preload_enabled:
        try:
            await asyncio.to_thread(get_knowledge_qa_service)
        except Exception:
            logger.exception("local model preload failed")
            if settings.model_preload_fail_fast:
                raise
    try:
        yield
    finally:
        await dispose_database_engines()
        logger.info("application stopped")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "多格式文档解析、结构感知父子切块、Dense+BM25 混合检索、"
        "Reranker、证据门控与可追溯引用。"
    ),
    lifespan=lifespan,
)
app.mount(
    "/static",
    StaticFiles(directory=PROJECT_ROOT / "app" / "static"),
    name="static",
)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(pilot_router)


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError):
    request_id = request_id_context.get()
    logger.warning("application error code=%s", exc.code)
    headers = {
        "X-Request-ID": request_id,
        "Cache-Control": "no-store",
    }
    if exc.status_code == 401:
        headers["WWW-Authenticate"] = "Session"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.public_message,
                "request_id": request_id,
            }
        },
        headers=headers,
    )


@app.middleware("http")
async def log_http_request(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    request_token = request_id_context.set(request_id)
    run_token = run_id_context.set("-")
    started_at = perf_counter()
    try:
        policy = _pilot_rate_policy(request)
        retry_after = None
        if policy is not None:
            scope, limit, window_seconds = policy
            retry_after = pilot_rate_limiter.check(
                scope=scope,
                client_key=_pilot_client_key(request),
                limit=limit,
                window_seconds=window_seconds,
            )
        if retry_after is not None:
            response = JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "请求过于频繁，请稍后重试。",
                        "request_id": request_id,
                    }
                },
                headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
            )
        else:
            response = await call_next(request)
    except Exception:
        duration_ms = (perf_counter() - started_at) * 1000
        pilot_runtime_metrics.record(status_code=500, duration_ms=duration_ms)
        logger.exception(
            "request failed method=%s path=%s",
            request.method,
            request.url.path,
        )
        raise
    else:
        duration_ms = (perf_counter() - started_at) * 1000
        pilot_runtime_metrics.record(
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        content_type = response.headers.get("Content-Type", "")
        if (
            content_type.lower().startswith("application/json")
            and "charset=" not in content_type.lower()
        ):
            response.headers["Content-Type"] = "application/json; charset=utf-8"
        response.headers["X-Request-ID"] = request_id
        _apply_security_headers(request, response)
        logger.info(
            "request completed method=%s path=%s status_code=%s duration_ms=%.2f",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
    finally:
        run_id_context.reset(run_token)
        request_id_context.reset(request_token)


@app.get("/", include_in_schema=False)
async def root(
    context: AuthContext | None = Depends(get_optional_auth_context),
):
    if context is None:
        return RedirectResponse("/login?next=%2F", status_code=303)
    if context.user.role == ROLE_ADMIN:
        return RedirectResponse("/admin", status_code=303)
    return FileResponse(PROJECT_ROOT / "app" / "static" / "index.html")


@app.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(PROJECT_ROOT / "app" / "static" / "login.html")


@app.get("/admin", include_in_schema=False)
async def admin_page(
    context: AuthContext | None = Depends(get_optional_auth_context),
):
    if context is None:
        return RedirectResponse("/login?next=%2Fadmin", status_code=303)
    if context.user.role != ROLE_ADMIN:
        return RedirectResponse("/", status_code=303)
    return FileResponse(PROJECT_ROOT / "app" / "static" / "admin.html")


@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "service": settings.app_name}


def check_readiness() -> tuple[dict[str, bool], int]:
    runtime = model_runtime_status.snapshot()
    models_ready = (
        runtime.ready if settings.model_preload_enabled else runtime.state != "failed"
    )
    checks = {
        "deepseek": bool(settings.deepseek_api_key),
        "milvus": False,
        "models": models_ready,
    }
    row_count = 0
    store = None
    try:
        store = MilvusKnowledgeStore(settings)
        if store.client.has_collection(settings.milvus_collection):
            row_count = store.collection_row_count()
            checks["milvus"] = row_count > 0
    except Exception:
        logger.exception("readiness check failed dependency=milvus")
    finally:
        if store is not None:
            store.close()
    return checks, row_count


@app.get("/api/v1/health/ready", response_model=ReadinessResult)
async def readiness(response: Response) -> ReadinessResult:
    checks, row_count = await asyncio.to_thread(check_readiness)
    checks["mysql"] = await check_mysql_connection(settings)
    ready = all(checks.values())
    if not ready:
        response.status_code = 503
    return ReadinessResult(
        status="ready" if ready else "not_ready",
        checks=checks,
        knowledge_chunks=row_count,
    )


def get_workflow() -> RAGWorkflow:
    return get_rag_workflow()


def get_agent_run_store() -> AgentRunStore:
    return AgentRunStore()


def get_conversation_store() -> ConversationStore:
    return ConversationStore()


def get_feedback_store() -> FeedbackStore:
    return FeedbackStore()


def get_indexing_job_store() -> IndexingJobStore:
    return IndexingJobStore()


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
    background_tasks: BackgroundTasks,
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
    background_tasks.add_task(
        execute_agent_run,
        run_id=run_id,
        conversation_id=run.conversation_id,
        user_id=context.user.user_id,
        question=payload.question,
        top_k=payload.top_k,
        workflow=workflow,
        store=store,
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


@app.post(
    "/api/v1/knowledge/uploads",
    response_model=KnowledgeUploadBatchCreated,
    status_code=202,
)
async def upload_knowledge_files(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    recreate: bool = Form(default=False),
    context: AuthContext = Depends(require_admin_csrf),
    store: IndexingJobStore = Depends(get_indexing_job_store),
) -> KnowledgeUploadBatchCreated:
    batch = await stage_knowledge_uploads(files, settings)
    try:
        jobs = await store.create_batch(
            batch,
            created_by_user_id=context.user.user_id,
            recreate=recreate,
        )
    except Exception:
        discard_staged_batch(batch)
        raise
    background_tasks.add_task(execute_indexing_batch, batch.batch_id, store)
    return KnowledgeUploadBatchCreated(
        batch_id=batch.batch_id,
        status_url=f"/api/v1/knowledge/uploads/{batch.batch_id}",
        items=[
            KnowledgeUploadItemCreated(
                job_id=job.job_id,
                filename=job.original_filename or Path(job.source_path).name,
                status_url=f"/api/v1/knowledge/index/{job.job_id}",
            )
            for job in jobs
        ],
    )


@app.get("/api/v1/knowledge/documents", response_model=KnowledgeDocumentList)
async def list_knowledge_documents(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _context: AuthContext = Depends(require_admin),
    store: IndexingJobStore = Depends(get_indexing_job_store),
) -> KnowledgeDocumentList:
    """查看已经进入知识库的文档和父子块数量。"""
    return KnowledgeDocumentList(
        items=await store.list_documents(limit=limit, offset=offset),
        limit=limit,
        offset=offset,
    )


@app.get(
    "/api/v1/knowledge/uploads/{batch_id}",
    response_model=KnowledgeUploadBatchDetail,
)
async def get_knowledge_upload_batch(
    batch_id: str,
    _context: AuthContext = Depends(require_admin),
    store: IndexingJobStore = Depends(get_indexing_job_store),
) -> KnowledgeUploadBatchDetail:
    jobs = await store.get_batch(batch_id)
    statuses = {job.status for job in jobs}
    completed_count = sum(job.status == "completed" for job in jobs)
    failed_count = sum(job.status == "failed" for job in jobs)
    if statuses & {"pending", "running"}:
        status = "running" if statuses != {"pending"} else "pending"
    elif failed_count == len(jobs):
        status = "failed"
    elif failed_count:
        status = "partial_failed"
    else:
        status = "completed"
    started = [job.started_at for job in jobs if job.started_at]
    completed = [job.completed_at for job in jobs if job.completed_at]
    terminal = status in {"completed", "partial_failed", "failed"}
    return KnowledgeUploadBatchDetail(
        batch_id=batch_id,
        status=status,
        file_count=len(jobs),
        completed_count=completed_count,
        failed_count=failed_count,
        document_count=sum(job.document_count for job in jobs),
        chunk_count=sum(job.chunk_count for job in jobs),
        created_at=min(job.created_at for job in jobs),
        started_at=min(started) if started else None,
        completed_at=max(completed) if terminal and completed else None,
        items=[
            KnowledgeUploadItemDetail(
                job_id=job.job_id,
                filename=job.original_filename or Path(job.source_path).name,
                status=job.status,
                file_size_bytes=job.file_size_bytes or 0,
                document_count=job.document_count,
                chunk_count=job.chunk_count,
                error="知识索引任务执行失败，请检查服务日志。"
                if job.status == "failed"
                else None,
                created_at=job.created_at,
                started_at=job.started_at,
                completed_at=job.completed_at,
            )
            for job in jobs
        ],
    )


@app.post(
    "/api/v1/knowledge/index",
    response_model=KnowledgeIndexCreated,
    status_code=202,
)
async def create_knowledge_index(
    payload: KnowledgeIndexCreate,
    background_tasks: BackgroundTasks,
    context: AuthContext = Depends(require_admin_csrf),
    store: IndexingJobStore = Depends(get_indexing_job_store),
) -> KnowledgeIndexCreated:
    job = await store.create(
        created_by_user_id=context.user.user_id,
        source_file=payload.source_file,
        recreate=payload.recreate,
    )
    background_tasks.add_task(execute_indexing_job, job.job_id, store)
    return KnowledgeIndexCreated(
        job_id=job.job_id,
        status_url=f"/api/v1/knowledge/index/{job.job_id}",
    )


@app.get(
    "/api/v1/knowledge/index/{job_id}",
    response_model=KnowledgeIndexDetail,
)
async def get_knowledge_index(
    job_id: str,
    _context: AuthContext = Depends(require_admin),
    store: IndexingJobStore = Depends(get_indexing_job_store),
) -> KnowledgeIndexDetail:
    job = await store.get(job_id)
    return KnowledgeIndexDetail(
        job_id=job.job_id,
        status=job.status,
        source_file=job.original_filename or Path(job.source_path).name,
        recreate=job.recreate,
        document_count=job.document_count,
        chunk_count=job.chunk_count,
        error="知识索引任务执行失败，请检查服务日志。"
        if job.status == "failed"
        else None,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )
