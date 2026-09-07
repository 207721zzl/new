"""RAG 运行、会话、反馈与知识索引仓储。"""

from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AgentRun,
    Conversation,
    EvaluationCase,
    IndexingJob,
    KnowledgeChunkRecord,
    KnowledgeDocumentRecord,
    KnowledgeParentChunkRecord,
    Message,
    MetricDefinition,
    Order,
    Payment,
    ToolCall,
    UserFeedback,
)
from app.errors import ConversationBusyError, ConversationNotFoundError
from app.rag.models import KnowledgeChunk, KnowledgeDocument, ParentKnowledgeChunk


def build_gmv_query(
    start_at: datetime,
    end_at: datetime,
    region: str | None = None,
) -> Select:
    """构建成功支付 GMV 查询；结束时间使用左闭右开区间。"""
    statement = (
        select(func.coalesce(func.sum(Payment.paid_amount), 0))
        .join(Order, Order.id == Payment.order_id)
        .where(
            Payment.payment_status == "success",
            Payment.paid_at >= start_at,
            Payment.paid_at < end_at,
        )
    )
    if region:
        statement = statement.where(Order.region == region)
    return statement


class MetricDefinitionRepository:
    """读取启用中的指标语义定义。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_code(self, metric_code: str) -> MetricDefinition | None:
        """按稳定指标编码读取单条启用定义。"""
        statement = select(MetricDefinition).where(
            MetricDefinition.metric_code == metric_code,
            MetricDefinition.is_active.is_(True),
        )
        return await self.session.scalar(statement)

    async def list_active(self) -> list[MetricDefinition]:
        """按指标编码返回全部启用定义。"""
        statement = (
            select(MetricDefinition)
            .where(MetricDefinition.is_active.is_(True))
            .order_by(MetricDefinition.metric_code)
        )
        result = await self.session.scalars(statement)
        return list(result)


class BusinessMetricRepository:
    """提供不经过 LLM 的可信基础指标查询。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def gmv(
        self,
        start_at: datetime,
        end_at: datetime,
        region: str | None = None,
    ) -> Decimal:
        """计算指定时间和可选区域的成功支付 GMV。"""
        value = await self.session.scalar(build_gmv_query(start_at, end_at, region))
        return Decimal(value or 0).quantize(Decimal("0.01"))


class AgentRunRepository:
    """持久化后台运行状态、结构化结果和可重放事件。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        run_id: str,
        conversation_id: str,
        turn_index: int,
        question: str,
        top_k: int | None,
        initial_event: dict[str, Any],
    ) -> AgentRun:
        run = AgentRun(
            run_id=run_id,
            conversation_id=conversation_id,
            turn_index=turn_index,
            question=question,
            top_k=top_k,
            status="queued",
            current_node="queued",
            events=[initial_event],
        )
        self.session.add(run)
        await self.session.commit()
        return run

    async def get(self, run_id: str) -> AgentRun | None:
        return await self.session.get(AgentRun, run_id)

    async def get_for_user(self, run_id: str, user_id: str) -> AgentRun | None:
        """只返回属于指定用户会话的运行，避免通过 UUID 越权读取。"""
        return await self.session.scalar(
            select(AgentRun)
            .join(
                Conversation,
                Conversation.conversation_id == AgentRun.conversation_id,
            )
            .where(AgentRun.run_id == run_id, Conversation.user_id == user_id)
        )

    async def mark_running(
        self,
        run_id: str,
        *,
        event: dict[str, Any],
    ) -> AgentRun | None:
        run = await self.get(run_id)
        if run is None:
            return None
        run.status = "running"
        run.current_node = "workflow"
        run.started_at = datetime.now(UTC).replace(tzinfo=None)
        run.events = [*run.events, event]
        await self.session.commit()
        return run

    async def record_node(
        self,
        run_id: str,
        *,
        node: str,
        intent: str | None,
        event: dict[str, Any],
    ) -> AgentRun | None:
        run = await self.get(run_id)
        if run is None:
            return None
        run.current_node = node
        if intent is not None:
            run.intent = intent
        run.events = [*run.events, event]
        await self.session.commit()
        return run

    async def complete(
        self,
        run_id: str,
        *,
        result: dict[str, Any],
        intent: str,
        event: dict[str, Any],
        commit: bool = True,
    ) -> AgentRun | None:
        run = await self.get(run_id)
        if run is None:
            return None
        run.status = "completed"
        run.current_node = "completed"
        run.intent = intent
        run.result = result
        run.completed_at = datetime.now(UTC).replace(tzinfo=None)
        run.events = [*run.events, event]
        if commit:
            await self.session.commit()
        return run

    async def fail(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        event: dict[str, Any],
        commit: bool = True,
    ) -> AgentRun | None:
        run = await self.get(run_id)
        if run is None:
            return None
        run.status = "failed"
        run.current_node = "failed"
        run.error_code = error_code
        run.error_message = error_message
        run.completed_at = datetime.now(UTC).replace(tzinfo=None)
        run.events = [*run.events, event]
        if commit:
            await self.session.commit()
        return run


class ConversationRepository:
    """维护每次 RAG 运行对应的会话和消息历史。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_turn(
        self,
        *,
        user_id: str,
        requested_conversation_id: str | None,
        message_id: str,
        run_id: str,
        question: str,
        top_k: int | None,
        initial_event: dict[str, Any],
    ) -> AgentRun:
        """锁定已有会话，并原子创建本轮运行与用户消息。"""
        if requested_conversation_id:
            conversation = await self.session.scalar(
                select(Conversation)
                .where(
                    Conversation.conversation_id == requested_conversation_id,
                    Conversation.user_id == user_id,
                )
                .with_for_update()
            )
            if conversation is None:
                raise ConversationNotFoundError()
            active_run = await self.session.scalar(
                select(AgentRun.run_id).where(
                    AgentRun.conversation_id == requested_conversation_id,
                    AgentRun.status.in_(("queued", "running")),
                )
            )
            if active_run is not None:
                raise ConversationBusyError()
            conversation_id = requested_conversation_id
            latest_turn = await self.session.scalar(
                select(func.max(AgentRun.turn_index)).where(
                    AgentRun.conversation_id == conversation_id
                )
            )
            turn_index = int(latest_turn or 0) + 1
        else:
            conversation_id = run_id
            turn_index = 1
            conversation = Conversation(
                conversation_id=conversation_id,
                user_id=user_id,
                title=question[:256],
            )
            self.session.add(conversation)
            # The models intentionally do not define ORM relationships.  Flush the
            # new parent row explicitly so MySQL never sees the AgentRun before its
            # Conversation when the unit of work is committed.
            await self.session.flush()

        run = AgentRun(
            run_id=run_id,
            conversation_id=conversation_id,
            turn_index=turn_index,
            question=question,
            top_k=top_k,
            status="queued",
            current_node="queued",
            events=[initial_event],
        )
        self.session.add(run)
        self.session.add(
            Message(
                message_id=message_id,
                conversation_id=conversation_id,
                run_id=run_id,
                role="user",
                content=question,
            )
        )
        conversation.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await self.session.commit()
        return run

    async def get(self, conversation_id: str) -> Conversation | None:
        """按主键读取会话。"""
        return await self.session.get(Conversation, conversation_id)

    async def get_for_user(
        self,
        conversation_id: str,
        user_id: str,
    ) -> Conversation | None:
        """读取指定用户拥有的会话，对不存在和越权统一返回空。"""
        return await self.session.scalar(
            select(Conversation).where(
                Conversation.conversation_id == conversation_id,
                Conversation.user_id == user_id,
            )
        )

    async def add_assistant_message(
        self,
        *,
        message_id: str,
        run_id: str,
        content: str,
    ) -> None:
        run = await self.session.get(AgentRun, run_id)
        if run is None:
            return
        conversation = await self.session.get(Conversation, run.conversation_id)
        if conversation is None:
            return
        existing = await self.session.scalar(
            select(Message).where(
                Message.run_id == run_id,
                Message.role == "assistant",
            )
        )
        if existing is None:
            self.session.add(
                Message(
                    message_id=message_id,
                    conversation_id=conversation.conversation_id,
                    run_id=run_id,
                    role="assistant",
                    content=content,
                )
            )
        conversation.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await self.session.commit()

    async def list_recent(
        self,
        *,
        user_id: str,
        limit: int,
        offset: int,
    ) -> list[Conversation]:
        result = await self.session.scalars(
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def list_messages(
        self,
        conversation_id: str,
        *,
        user_id: str,
    ) -> list[Message]:
        result = await self.session.scalars(
            select(Message)
            .join(AgentRun, AgentRun.run_id == Message.run_id)
            .join(
                Conversation,
                Conversation.conversation_id == Message.conversation_id,
            )
            .where(
                Message.conversation_id == conversation_id,
                Conversation.user_id == user_id,
            )
            .order_by(
                AgentRun.turn_index,
                case((Message.role == "user", 0), else_=1),
                Message.message_id,
            )
        )
        return list(result)

    async def list_context_messages(
        self,
        conversation_id: str,
        *,
        user_id: str,
        exclude_run_id: str,
        limit: int,
    ) -> list[Message]:
        """读取当前轮之前最近完成的消息，并恢复为时间正序。"""
        result = await self.session.scalars(
            select(Message)
            .join(AgentRun, AgentRun.run_id == Message.run_id)
            .join(
                Conversation,
                Conversation.conversation_id == Message.conversation_id,
            )
            .where(
                Message.conversation_id == conversation_id,
                Conversation.user_id == user_id,
                Message.run_id != exclude_run_id,
                AgentRun.status == "completed",
            )
            .order_by(
                AgentRun.turn_index.desc(),
                case((Message.role == "assistant", 0), else_=1),
                Message.message_id.desc(),
            )
            .limit(limit)
        )
        return list(reversed(list(result)))


class FeedbackRepository:
    """验证运行存在后保存用户反馈。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        user_id: str,
        feedback_id: str,
        run_id: str,
        rating: int,
        comment: str | None,
        correction: str | None,
    ) -> UserFeedback | None:
        owned_run_id = await self.session.scalar(
            select(AgentRun.run_id)
            .join(
                Conversation,
                Conversation.conversation_id == AgentRun.conversation_id,
            )
            .where(AgentRun.run_id == run_id, Conversation.user_id == user_id)
        )
        if owned_run_id is None:
            return None
        feedback = UserFeedback(
            feedback_id=feedback_id,
            run_id=run_id,
            rating=rating,
            comment=comment,
            correction=correction,
        )
        self.session.add(feedback)
        await self.session.commit()
        return feedback


class IndexingRepository:
    """维护索引任务以及 MySQL 文档/块真源。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_job(
        self,
        *,
        created_by_user_id: str,
        job_id: str,
        source_path: str,
        recreate: bool,
        batch_id: str | None = None,
        original_filename: str | None = None,
        file_size_bytes: int | None = None,
        content_sha256: str | None = None,
    ) -> IndexingJob:
        job = IndexingJob(
            job_id=job_id,
            created_by_user_id=created_by_user_id,
            batch_id=batch_id,
            source_path=source_path,
            original_filename=original_filename,
            file_size_bytes=file_size_bytes,
            content_sha256=content_sha256,
            recreate=recreate,
            status="pending",
        )
        self.session.add(job)
        await self.session.commit()
        return job

    async def get_job(self, job_id: str) -> IndexingJob | None:
        return await self.session.get(IndexingJob, job_id)

    async def list_documents(
        self,
        *,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        """返回知识文档及其父子块数量，不加载大段正文。"""
        parent_counts = (
            select(
                KnowledgeParentChunkRecord.document_id.label("document_id"),
                func.count().label("parent_count"),
            )
            .group_by(KnowledgeParentChunkRecord.document_id)
            .subquery()
        )
        child_counts = (
            select(
                KnowledgeChunkRecord.document_id.label("document_id"),
                func.count().label("child_count"),
            )
            .group_by(KnowledgeChunkRecord.document_id)
            .subquery()
        )
        rows = await self.session.execute(
            select(
                KnowledgeDocumentRecord,
                func.coalesce(parent_counts.c.parent_count, 0),
                func.coalesce(child_counts.c.child_count, 0),
            )
            .outerjoin(
                parent_counts,
                parent_counts.c.document_id == KnowledgeDocumentRecord.document_id,
            )
            .outerjoin(
                child_counts,
                child_counts.c.document_id == KnowledgeDocumentRecord.document_id,
            )
            .order_by(KnowledgeDocumentRecord.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return [
            {
                "document_id": document.document_id,
                "title": document.title,
                "doc_type": document.doc_type,
                "source": document.source,
                "version": document.version,
                "original_filename": document.original_filename,
                "mime_type": document.mime_type,
                "language": document.language,
                "index_status": document.index_status,
                "parent_chunk_count": int(parent_count),
                "child_chunk_count": int(child_count),
                "updated_at": document.updated_at,
            }
            for document, parent_count, child_count in rows.all()
        ]

    async def create_batch_jobs(
        self,
        *,
        created_by_user_id: str,
        batch_id: str,
        files: list[dict[str, Any]],
        recreate: bool,
    ) -> list[IndexingJob]:
        """在一个事务中为已落盘的上传文件创建索引任务。"""
        jobs = [
            IndexingJob(
                job_id=str(item["job_id"]),
                created_by_user_id=created_by_user_id,
                batch_id=batch_id,
                source_path=str(item["source_path"]),
                original_filename=str(item["original_filename"]),
                file_size_bytes=int(item["file_size_bytes"]),
                content_sha256=str(item["content_sha256"]),
                recreate=recreate and index == 0,
                status="pending",
            )
            for index, item in enumerate(files)
        ]
        self.session.add_all(jobs)
        await self.session.commit()
        return jobs

    async def list_batch_jobs(self, batch_id: str) -> list[IndexingJob]:
        """按安全执行顺序返回一个上传批次的全部索引任务。"""
        rows = await self.session.scalars(
            select(IndexingJob)
            .where(IndexingJob.batch_id == batch_id)
            .order_by(
                IndexingJob.recreate.desc(),
                IndexingJob.created_at.asc(),
                IndexingJob.job_id.asc(),
            )
        )
        return list(rows)

    async def mark_running(self, job_id: str) -> None:
        job = await self.get_job(job_id)
        if job is None:
            return
        job.status = "running"
        job.started_at = datetime.now(UTC).replace(tzinfo=None)
        job.error_message = None
        await self.session.commit()

    async def upsert_prepared(
        self,
        documents: list[KnowledgeDocument],
        parent_chunks: list[ParentKnowledgeChunk],
        chunks: list[KnowledgeChunk],
        *,
        recreate: bool = False,
    ) -> None:
        if recreate:
            # 这是显式重建请求；删除顺序遵循父子外键。
            await self.session.execute(delete(KnowledgeChunkRecord))
            await self.session.execute(delete(KnowledgeParentChunkRecord))
            await self.session.execute(delete(KnowledgeDocumentRecord))
        else:
            parent_ids_by_document: dict[str, list[str]] = {}
            child_ids_by_document: dict[str, list[str]] = {}
            for parent in parent_chunks:
                parent_ids_by_document.setdefault(parent.document_id, []).append(
                    parent.parent_chunk_id
                )
            for chunk in chunks:
                child_ids_by_document.setdefault(chunk.document_id, []).append(
                    chunk.chunk_id
                )
            for document in documents:
                document_id = document.document_id
                current_children = child_ids_by_document.get(document_id, [])
                current_parents = parent_ids_by_document.get(document_id, [])
                child_delete = delete(KnowledgeChunkRecord).where(
                    KnowledgeChunkRecord.document_id == document_id
                )
                if current_children:
                    child_delete = child_delete.where(
                        KnowledgeChunkRecord.chunk_id.not_in(current_children)
                    )
                await self.session.execute(child_delete)
                parent_delete = delete(KnowledgeParentChunkRecord).where(
                    KnowledgeParentChunkRecord.document_id == document_id
                )
                if current_parents:
                    parent_delete = parent_delete.where(
                        KnowledgeParentChunkRecord.parent_chunk_id.not_in(
                            current_parents
                        )
                    )
                await self.session.execute(parent_delete)

        for document in documents:
            source_updated_at = datetime.combine(document.updated_at, time.min)
            record = await self.session.get(
                KnowledgeDocumentRecord,
                document.document_id,
            )
            if record is None:
                record = KnowledgeDocumentRecord(document_id=document.document_id)
                self.session.add(record)
            record.title = document.title
            record.content = document.content
            record.doc_type = document.doc_type
            record.metric_name = document.metric_name
            record.source = document.source
            record.version = document.version
            record.source_updated_at = source_updated_at
            record.original_filename = document.original_filename
            record.mime_type = document.mime_type
            record.content_sha256 = document.content_sha256
            record.language = document.language
            record.metadata_json = document.metadata
            record.index_status = "pending"
            record.error_message = None

        for parent in parent_chunks:
            source_updated_at = datetime.combine(parent.updated_at, time.min)
            record = await self.session.get(
                KnowledgeParentChunkRecord,
                parent.parent_chunk_id,
            )
            if record is None:
                record = KnowledgeParentChunkRecord(
                    parent_chunk_id=parent.parent_chunk_id
                )
                self.session.add(record)
            record.document_id = parent.document_id
            record.title = parent.title
            record.content = parent.content
            record.parent_index = parent.parent_index
            record.doc_type = parent.doc_type
            record.metric_name = parent.metric_name
            record.source = parent.source
            record.version = parent.version
            record.source_updated_at = source_updated_at
            record.section_path = parent.section_path
            record.page_start = parent.page_start
            record.page_end = parent.page_end
            record.block_type = parent.block_type
            record.language = parent.language
            record.metadata_json = parent.metadata
            record.index_status = "pending"

        for chunk in chunks:
            source_updated_at = datetime.combine(chunk.updated_at, time.min)
            record = await self.session.get(KnowledgeChunkRecord, chunk.chunk_id)
            if record is None:
                record = KnowledgeChunkRecord(chunk_id=chunk.chunk_id)
                self.session.add(record)
            record.parent_chunk_id = chunk.parent_chunk_id
            record.document_id = chunk.document_id
            record.title = chunk.title
            record.content = chunk.content
            record.chunk_index = chunk.chunk_index
            record.doc_type = chunk.doc_type
            record.metric_name = chunk.metric_name
            record.source = chunk.source
            record.version = chunk.version
            record.source_updated_at = source_updated_at
            record.section_path = chunk.section_path
            record.page_start = chunk.page_start
            record.page_end = chunk.page_end
            record.block_type = chunk.block_type
            record.language = chunk.language
            record.index_status = "pending"
        await self.session.commit()

    async def complete_job(
        self,
        job_id: str,
        *,
        document_ids: list[str],
        parent_chunk_ids: list[str],
        chunk_ids: list[str],
    ) -> None:
        job = await self.get_job(job_id)
        if job is None:
            return
        documents = await self.session.scalars(
            select(KnowledgeDocumentRecord).where(
                KnowledgeDocumentRecord.document_id.in_(document_ids)
            )
        )
        chunks = await self.session.scalars(
            select(KnowledgeChunkRecord).where(
                KnowledgeChunkRecord.chunk_id.in_(chunk_ids)
            )
        )
        parent_chunks = await self.session.scalars(
            select(KnowledgeParentChunkRecord).where(
                KnowledgeParentChunkRecord.parent_chunk_id.in_(parent_chunk_ids)
            )
        )
        for document in documents:
            document.index_status = "completed"
            document.error_message = None
        for chunk in chunks:
            chunk.index_status = "completed"
        for parent in parent_chunks:
            parent.index_status = "completed"
        job.status = "completed"
        job.document_count = len(document_ids)
        job.chunk_count = len(chunk_ids)
        job.completed_at = datetime.now(UTC).replace(tzinfo=None)
        await self.session.commit()

    async def fail_job(self, job_id: str, error_message: str) -> None:
        job = await self.get_job(job_id)
        if job is None:
            return
        job.status = "failed"
        job.error_message = error_message[:2000]
        job.completed_at = datetime.now(UTC).replace(tzinfo=None)
        await self.session.commit()


class ToolCallRepository:
    """持久化 MCP 工具调用结果和耗时。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        tool_call_id: str,
        run_id: str | None,
        tool_name: str,
        input_payload: dict[str, Any],
    ) -> None:
        if run_id and await self.session.get(AgentRun, run_id) is None:
            run_id = None
        self.session.add(
            ToolCall(
                tool_call_id=tool_call_id,
                run_id=run_id,
                tool_name=tool_name,
                status="running",
                input_payload=input_payload,
            )
        )
        await self.session.commit()

    async def finish(
        self,
        tool_call_id: str,
        *,
        status: str,
        duration_ms: float,
        output_payload: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        call = await self.session.get(ToolCall, tool_call_id)
        if call is None:
            return
        call.status = status
        call.duration_ms = Decimal(str(round(duration_ms, 3)))
        call.output_payload = output_payload
        call.error_code = error_code
        await self.session.commit()


class EvaluationCaseRepository:
    """离线评测用例的数据库目录。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self,
        *,
        case_id: str,
        category: str,
        question: str,
        expected: dict[str, Any],
        version: str,
    ) -> None:
        case = await self.session.get(EvaluationCase, case_id)
        if case is None:
            case = EvaluationCase(case_id=case_id)
            self.session.add(case)
        case.category = category
        case.question = question
        case.expected = expected
        case.version = version
        case.is_active = True
        await self.session.commit()
