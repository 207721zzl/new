"""从 MySQL 状态真源批量恢复父块生成上下文。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import KnowledgeParentChunkRecord
from app.db.session import get_read_session_factory
from app.rag.models import ParentKnowledgeContext


class ParentChunkStore:
    """使用短生命周期只读会话批量读取父块。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.session_factory = session_factory or get_read_session_factory()

    async def get_many(
        self,
        parent_chunk_ids: list[str],
    ) -> dict[str, ParentKnowledgeContext]:
        unique_ids = list(dict.fromkeys(parent_chunk_ids))
        if not unique_ids:
            return {}
        async with self.session_factory() as session:
            rows = await session.scalars(
                select(KnowledgeParentChunkRecord).where(
                    KnowledgeParentChunkRecord.parent_chunk_id.in_(unique_ids),
                    KnowledgeParentChunkRecord.index_status == "completed",
                )
            )
            records = list(rows)
        return {
            record.parent_chunk_id: ParentKnowledgeContext(
                parent_chunk_id=record.parent_chunk_id,
                document_id=record.document_id,
                title=record.title,
                content=record.content,
                parent_index=record.parent_index,
                source=record.source,
                version=record.version,
                updated_at=record.source_updated_at.date().isoformat(),
                section_path=tuple(record.section_path or []),
                page_start=record.page_start,
                page_end=record.page_end,
                block_type=record.block_type,
            )
            for record in records
        }
