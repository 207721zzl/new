"""EvidenceRAG FastAPI 应用入口。"""

from pathlib import Path

from fastapi import (
    Depends,
    File,
    Form,
    Query,
    UploadFile,
)

from packages.contracts.rag import SearchRequest, encode_evidence
from packages.platform.auth import (
    require_admin,
    require_admin_csrf,
)
from packages.platform.auth import AuthContext
from app.config import get_settings
from services.knowledge.store import IndexingJobStore
from app.operations.uploads import discard_staged_batch, stage_knowledge_uploads
from app.schemas import (
    KnowledgeIndexCreate,
    KnowledgeIndexCreated,
    KnowledgeIndexDetail,
    KnowledgeDocumentList,
    KnowledgeUploadBatchCreated,
    KnowledgeUploadBatchDetail,
    KnowledgeUploadItemCreated,
    KnowledgeUploadItemDetail,
)
from packages.platform.application import create_app
from services.knowledge.deletion import delete_document
from services.knowledge.operations import router as operations_router
from services.knowledge.retrieval import retrieve

settings = get_settings()
app = create_app("knowledge")
app.include_router(operations_router)


@app.post("/internal/v1/search")
async def internal_search(payload: SearchRequest):
    return {"evidence": encode_evidence(await retrieve(payload.query, payload.top_k))}


@app.delete("/api/v1/admin/knowledge/documents/{document_id}")
async def remove_document(
    document_id: str, context: AuthContext = Depends(require_admin_csrf)
):
    return await delete_document(document_id, context.user.user_id)


def get_indexing_job_store() -> IndexingJobStore:
    return IndexingJobStore()


@app.post(
    "/api/v1/knowledge/uploads",
    response_model=KnowledgeUploadBatchCreated,
    status_code=202,
)
async def upload_knowledge_files(
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
    discard_staged_batch(batch)
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
    context: AuthContext = Depends(require_admin_csrf),
    store: IndexingJobStore = Depends(get_indexing_job_store),
) -> KnowledgeIndexCreated:
    job = await store.create(
        created_by_user_id=context.user.user_id,
        source_file=payload.source_file,
        recreate=payload.recreate,
    )
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
