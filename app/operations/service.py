"""RAG 会话、反馈与索引任务的短生命周期应用服务。"""

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import PROJECT_ROOT, Settings, get_settings
from app.db.models import IndexingJob
from app.db.repositories import (
    ConversationRepository,
    FeedbackRepository,
    IndexingRepository,
)
from app.db.session import get_admin_session_factory
from app.errors import IndexJobNotFoundError, KnowledgeIndexRequestError
from app.errors import KnowledgeUploadBatchNotFoundError
from app.logging_config import get_logger
from app.operations.uploads import StagedKnowledgeBatch
from app.ingestion import SUPPORTED_EXTENSIONS, parse_source
from app.rag.documents import chunk_documents
from app.rag.embeddings import get_embedder
from app.rag.milvus_store import MilvusKnowledgeStore
from app.rag.models import KnowledgeChunk


logger = get_logger("operations.service")


class ConversationStore:
    """读取持久化会话及其消息。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.session_factory = session_factory or get_admin_session_factory()

    async def list_recent(
        self,
        *,
        user_id: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            repository = ConversationRepository(session)
            conversations = await repository.list_recent(
                user_id=user_id,
                limit=limit,
                offset=offset,
            )
            result: list[dict[str, Any]] = []
            for conversation in conversations:
                messages = await repository.list_messages(
                    conversation.conversation_id,
                    user_id=user_id,
                )
                result.append(self._serialize(conversation, messages))
            return result

    async def get(
        self,
        conversation_id: str,
        *,
        user_id: str,
    ) -> dict[str, Any] | None:
        """读取一个会话的全部持久化消息。"""
        async with self.session_factory() as session:
            repository = ConversationRepository(session)
            conversation = await repository.get_for_user(conversation_id, user_id)
            if conversation is None:
                return None
            messages = await repository.list_messages(
                conversation_id,
                user_id=user_id,
            )
            return self._serialize(conversation, messages)

    @staticmethod
    def _serialize(conversation, messages) -> dict[str, Any]:
        latest_run_id = messages[-1].run_id if messages else None
        return {
            "conversation_id": conversation.conversation_id,
            # 保留 run_id 字段兼容既有列表消费者，语义调整为最新一轮运行。
            "run_id": latest_run_id,
            "title": conversation.title,
            "messages": [
                {
                    "message_id": message.message_id,
                    "run_id": message.run_id,
                    "role": message.role,
                    "content": message.content,
                    "created_at": message.created_at,
                }
                for message in messages
            ],
            "created_at": conversation.created_at,
            "updated_at": conversation.updated_at,
        }


class FeedbackStore:
    """保存经过运行存在性校验的用户反馈。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.session_factory = session_factory or get_admin_session_factory()

    async def create(
        self,
        *,
        user_id: str,
        run_id: str,
        rating: int,
        comment: str | None,
        correction: str | None,
    ) -> str | None:
        feedback_id = str(uuid4())
        async with self.session_factory() as session:
            feedback = await FeedbackRepository(session).create(
                user_id=user_id,
                feedback_id=feedback_id,
                run_id=run_id,
                rating=rating,
                comment=comment,
                correction=correction,
            )
        return feedback_id if feedback is not None else None


class IndexingJobStore:
    """创建、查询和执行幂等知识索引任务。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.session_factory = session_factory or get_admin_session_factory()

    def resolve_source(self, source_file: str | None) -> Path:
        """只允许索引项目 data/knowledge 下的受支持知识文件。"""
        allowed_root = (PROJECT_ROOT / "data" / "knowledge").resolve()
        if source_file:
            raw_path = Path(source_file)
            candidate = (
                raw_path.resolve()
                if raw_path.is_absolute()
                else (allowed_root / raw_path).resolve()
            )
        else:
            candidate = self.settings.resolved_knowledge_source_file.resolve()
        if not candidate.is_relative_to(allowed_root):
            raise KnowledgeIndexRequestError("knowledge source is outside allowlist")
        if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS or not candidate.is_file():
            raise KnowledgeIndexRequestError("knowledge source type is not supported")
        return candidate

    async def create(
        self,
        *,
        created_by_user_id: str,
        source_file: str | None,
        recreate: bool,
    ) -> IndexingJob:
        source_path = self.resolve_source(source_file)
        job_id = str(uuid4())
        async with self.session_factory() as session:
            return await IndexingRepository(session).create_job(
                created_by_user_id=created_by_user_id,
                job_id=job_id,
                source_path=str(source_path),
                recreate=recreate,
            )

    async def create_batch(
        self,
        batch: StagedKnowledgeBatch,
        *,
        created_by_user_id: str,
        recreate: bool,
    ) -> list[IndexingJob]:
        """为同一上传批次的全部文件原子创建索引任务。"""
        prepared_files = []
        for item in batch.files:
            source_path = self.resolve_source(str(item.source_path))
            prepared_files.append(
                {
                    "job_id": str(uuid4()),
                    "source_path": str(source_path),
                    "original_filename": item.original_filename,
                    "file_size_bytes": item.file_size_bytes,
                    "content_sha256": item.content_sha256,
                }
            )
        async with self.session_factory() as session:
            return await IndexingRepository(session).create_batch_jobs(
                created_by_user_id=created_by_user_id,
                batch_id=batch.batch_id,
                files=prepared_files,
                recreate=recreate,
            )

    async def get(self, job_id: str) -> IndexingJob:
        async with self.session_factory() as session:
            job = await IndexingRepository(session).get_job(job_id)
        if job is None:
            raise IndexJobNotFoundError()
        return job

    async def get_batch(self, batch_id: str) -> list[IndexingJob]:
        """读取上传批次；没有任何关联任务时返回稳定 404。"""
        async with self.session_factory() as session:
            jobs = await IndexingRepository(session).list_batch_jobs(batch_id)
        if not jobs:
            raise KnowledgeUploadBatchNotFoundError()
        return jobs

    async def list_documents(
        self,
        *,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            return await IndexingRepository(session).list_documents(
                limit=limit,
                offset=offset,
            )

    async def execute(self, job_id: str) -> None:
        job = await self.get(job_id)
        try:
            async with self.session_factory() as session:
                await IndexingRepository(session).mark_running(job_id)
            documents = await asyncio.to_thread(
                parse_source,
                Path(job.source_path),
                original_filename=job.original_filename
                or Path(job.source_path).name,
                pdf_ocr_enabled=self.settings.pdf_ocr_enabled,
                pdf_ocr_language=self.settings.pdf_ocr_language,
                pdf_ocr_dpi=self.settings.pdf_ocr_dpi,
                excel_rows_per_block=self.settings.excel_rows_per_block,
            )
            chunking = chunk_documents(
                documents,
                parent_chunk_size=self.settings.knowledge_parent_chunk_size,
                parent_overlap=self.settings.knowledge_parent_chunk_overlap,
                child_chunk_size=self.settings.knowledge_child_chunk_size,
                child_overlap=self.settings.knowledge_child_chunk_overlap,
            )
            async with self.session_factory() as session:
                await IndexingRepository(session).upsert_prepared(
                    documents,
                    chunking.parents,
                    chunking.children,
                    recreate=job.recreate,
                )
            await asyncio.to_thread(
                self._write_vectors,
                chunking.children,
                job.recreate,
            )
            async with self.session_factory() as session:
                await IndexingRepository(session).complete_job(
                    job_id,
                    document_ids=[item.document_id for item in documents],
                    parent_chunk_ids=[
                        item.parent_chunk_id for item in chunking.parents
                    ],
                    chunk_ids=[item.chunk_id for item in chunking.children],
                )
            logger.info(
                "indexing job completed job_id=%s documents=%s parents=%s children=%s",
                job_id,
                len(documents),
                len(chunking.parents),
                len(chunking.children),
            )
        except Exception as exc:
            logger.exception("indexing job failed job_id=%s", job_id)
            async with self.session_factory() as session:
                await IndexingRepository(session).fail_job(
                    job_id,
                    f"{type(exc).__name__}: {exc}",
                )

    def _write_vectors(
        self,
        chunks: list[KnowledgeChunk],
        recreate: bool,
    ) -> None:
        embedder = get_embedder()
        store = MilvusKnowledgeStore(self.settings)
        try:
            store.ensure_collection(recreate=recreate)
            if not recreate:
                store.delete_documents(
                    list(dict.fromkeys(chunk.document_id for chunk in chunks))
                )
            batch_size = self.settings.knowledge_index_batch_size
            for start in range(0, len(chunks), batch_size):
                chunk_batch = chunks[start : start + batch_size]
                vectors = embedder.encode(
                    [chunk.embedding_text for chunk in chunk_batch]
                )
                store.upsert_chunks(chunk_batch, vectors)
        finally:
            store.close()

    async def execute_batch(self, batch_id: str) -> None:
        """顺序执行上传批次，确保 recreate 只在第一个任务发生。"""
        jobs = await self.get_batch(batch_id)
        for job in jobs:
            await self.execute(job.job_id)


async def execute_indexing_job(job_id: str, store: IndexingJobStore) -> None:
    """BackgroundTasks 使用的稳定入口。"""
    await store.execute(job_id)


async def execute_indexing_batch(batch_id: str, store: IndexingJobStore) -> None:
    """BackgroundTasks 使用的批量索引入口。"""
    await store.execute_batch(batch_id)
