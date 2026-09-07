"""经校验 SQL 的 MySQL 只读执行器。"""

import asyncio
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.models import QueryExecutionResult, ValidatedSQL
from app.config import Settings, get_settings
from app.errors import ConfigurationError, SQLExecutionError
from app.logging_config import get_logger


logger = get_logger("analysis.executor")


class ReadOnlySQLExecutor:
    """在只读会话中执行 EXPLAIN 和带服务端超时的 SELECT。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if self.execution_user != self.settings.database_read_expected_user:
            raise ConfigurationError("DATABASE_READ_URL is not the expected reader")

    @property
    def execution_user(self) -> str:
        """返回实际配置的只读连接用户名，不暴露其他连接信息。"""
        return make_url(self.settings.database_read_url_value).username or "unknown"

    async def execute(
        self,
        session: AsyncSession,
        query: ValidatedSQL,
    ) -> QueryExecutionResult:
        """先 EXPLAIN，再在 MySQL 与协程双重超时下执行查询。"""
        timeout_ms = int(self.settings.sql_query_timeout_seconds * 1000)
        try:
            await session.execute(
                text("SET SESSION MAX_EXECUTION_TIME = :timeout_ms"),
                {"timeout_ms": timeout_ms},
            )
            async with asyncio.timeout(self.settings.sql_query_timeout_seconds):
                explain_result = await session.execute(text(f"EXPLAIN {query.sql}"))
                explain = [
                    self._json_safe_mapping(row)
                    for row in explain_result.mappings().all()
                ]
                result = await session.execute(text(query.sql))
                columns = list(result.keys())
                rows = [
                    self._json_safe_mapping(row)
                    for row in result.mappings().fetchmany(query.limit)
                ]
        except (TimeoutError, SQLAlchemyError) as exc:
            logger.warning("read-only SQL execution failed error_type=%s", type(exc).__name__)
            raise SQLExecutionError() from exc

        return QueryExecutionResult(columns=columns, rows=rows, explain=explain)

    @classmethod
    def _json_safe_mapping(cls, row: Any) -> dict[str, Any]:
        return {str(key): cls._json_safe(value) for key, value in dict(row).items()}

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value
