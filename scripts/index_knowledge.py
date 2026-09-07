"""校验、切块或持久化索引任一受支持的知识文件。"""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select

from app.auth.constants import ROLE_ADMIN, USER_STATUS_ACTIVE
from app.auth.security import validate_username
from app.config import get_settings
from app.db.models import User
from app.db.session import dispose_database_engines
from app.db.session import get_admin_session_factory
from app.ingestion import parse_source
from app.logging_config import get_logger
from app.operations.service import IndexingJobStore
from app.rag.documents import chunk_documents
from app.rag.milvus_store import MilvusKnowledgeStore


logger = get_logger("scripts.index_knowledge")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse and index a PDF, DOCX, Markdown, HTML, TXT, Excel or JSON file.",
    )
    parser.add_argument("--source", type=Path, help="Knowledge file under data/knowledge.")
    parser.add_argument("--recreate", action="store_true", help="Recreate the Milvus collection first.")
    parser.add_argument(
        "--actor-username",
        default="admin",
        help="Active administrator recorded as the indexing operator (default: admin).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Parse and chunk without external services.")
    mode.add_argument("--schema-only", action="store_true", help="Only create the Milvus schema.")
    return parser.parse_args()


def prepare(source_path: Path):
    settings = get_settings()
    documents = parse_source(
        source_path,
        pdf_ocr_enabled=settings.pdf_ocr_enabled,
        pdf_ocr_language=settings.pdf_ocr_language,
        pdf_ocr_dpi=settings.pdf_ocr_dpi,
        excel_rows_per_block=settings.excel_rows_per_block,
    )
    chunks = chunk_documents(
        documents,
        parent_chunk_size=settings.knowledge_parent_chunk_size,
        parent_overlap=settings.knowledge_parent_chunk_overlap,
        child_chunk_size=settings.knowledge_child_chunk_size,
        child_overlap=settings.knowledge_child_chunk_overlap,
    )
    logger.info(
        "knowledge prepared source=%s documents=%s parents=%s children=%s",
        source_path,
        len(documents),
        len(chunks.parents),
        len(chunks.children),
    )
    return documents, chunks


async def resolve_active_admin_user_id(username: str) -> str:
    """Resolve the accountable administrator for a persistent indexing job."""
    normalized_username = validate_username(username)
    session_factory = get_admin_session_factory()
    async with session_factory() as session:
        user_id = await session.scalar(
            select(User.user_id).where(
                User.normalized_username == normalized_username,
                User.role == ROLE_ADMIN,
                User.status == USER_STATUS_ACTIVE,
            )
        )
    if user_id is None:
        raise RuntimeError(
            f"active administrator '{normalized_username}' was not found; "
            "create/activate it first or pass --actor-username"
        )
    return user_id


async def run_persistent_job(
    source_path: Path,
    recreate: bool,
    actor_username: str,
) -> int:
    store = IndexingJobStore()
    try:
        actor_user_id = await resolve_active_admin_user_id(actor_username)
        job = await store.create(
            created_by_user_id=actor_user_id,
            source_file=str(source_path.resolve()),
            recreate=recreate,
        )
        await store.execute(job.job_id)
        completed = await store.get(job.job_id)
        print(
            f"job_id={completed.job_id} status={completed.status} "
            f"documents={completed.document_count} chunks={completed.chunk_count}"
        )
        return 0 if completed.status == "completed" else 1
    finally:
        await dispose_database_engines()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    source_path = (args.source or settings.resolved_knowledge_source_file).resolve()
    if args.schema_only:
        store = MilvusKnowledgeStore(settings)
        try:
            store.ensure_collection(recreate=args.recreate)
        finally:
            store.close()
        return 0
    prepare(source_path)
    if args.dry_run:
        return 0
    return asyncio.run(
        run_persistent_job(source_path, args.recreate, args.actor_username)
    )


if __name__ == "__main__":
    raise SystemExit(main())
