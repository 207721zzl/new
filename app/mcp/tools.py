"""可脱离 MCP 传输层单独测试的工具业务实现。"""

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.executor import ReadOnlySQLExecutor
from app.analysis.safety import SQLSafetyValidator
from app.analysis.schema import BUSINESS_SCHEMA
from app.config import Settings, get_settings
from app.db.session import get_read_session_factory
from app.forecast.service import SalesForecastService
from app.logging_config import get_logger
from app.mcp.models import (
    ReadOnlySQLRequest,
    ReadOnlySQLResponse,
    SalesForecastToolRequest,
    SalesForecastToolResponse,
    TableColumnResult,
    TableSchemaRequest,
    TableSchemaResponse,
    TableSchemaResult,
)
from app.operations.service import ToolCallStore


logger = get_logger("mcp.tools")


class InsightAgentToolService:
    """复用应用安全边界的三个标准 MCP 工具。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        sql_executor: ReadOnlySQLExecutor | None = None,
        forecast_service: SalesForecastService | None = None,
        audit_store: ToolCallStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.session_factory = session_factory or get_read_session_factory()
        self.sql_executor = sql_executor or ReadOnlySQLExecutor(self.settings)
        self.sql_validator = SQLSafetyValidator(self.settings.sql_query_limit)
        self.forecast_service = forecast_service or SalesForecastService(self.settings)
        self.audit_store = audit_store

    async def _start_audit(
        self,
        tool_name: str,
        payload: dict,
    ) -> tuple[str, float] | None:
        if self.audit_store is None:
            return None
        try:
            return await self.audit_store.start(tool_name, payload)
        except Exception:
            logger.exception("tool audit start failed tool=%s", tool_name)
            return None

    async def _finish_audit(
        self,
        audit: tuple[str, float] | None,
        *,
        output: dict | None = None,
        error: Exception | None = None,
    ) -> None:
        if self.audit_store is None or audit is None:
            return
        tool_call_id, started_at = audit
        try:
            if error is None and output is not None:
                await self.audit_store.succeed(tool_call_id, started_at, output)
            else:
                error_code = getattr(error, "code", type(error).__name__)
                await self.audit_store.fail(
                    tool_call_id,
                    started_at,
                    str(error_code),
                )
        except Exception:
            logger.exception("tool audit completion failed tool_call_id=%s", tool_call_id)

    async def get_table_schema(
        self,
        request: TableSchemaRequest,
    ) -> TableSchemaResponse:
        """返回 Text2SQL 与执行校验共用的唯一白名单 Schema。"""
        audit = await self._start_audit(
            "get_table_schema",
            request.model_dump(mode="json"),
        )
        try:
            requested = request.table_name.lower() if request.table_name else None
            selected = [
                table
                for table in BUSINESS_SCHEMA
                if requested is None or table.name == requested
            ]
            if requested is not None and not selected:
                raise ValueError(f"table is not allowlisted: {requested}")
            response = TableSchemaResponse(
                tables=[
                    TableSchemaResult(
                        name=table.name,
                        description=table.description,
                        columns=[
                            TableColumnResult(
                                name=column.name,
                                data_type=column.data_type,
                                description=column.description,
                            )
                            for column in table.columns
                        ],
                    )
                    for table in selected
                ]
            )
        except Exception as exc:
            await self._finish_audit(audit, error=exc)
            raise
        await self._finish_audit(audit, output=response.model_dump(mode="json"))
        return response

    async def execute_readonly_sql(
        self,
        request: ReadOnlySQLRequest,
    ) -> ReadOnlySQLResponse:
        """执行统一 SQLGlot 白名单、LIMIT、EXPLAIN 和只读账号约束。"""
        audit = await self._start_audit(
            "execute_readonly_sql",
            request.model_dump(mode="json"),
        )
        try:
            validated = self.sql_validator.validate(request.sql)
            async with self.session_factory() as session:
                result = await self.sql_executor.execute(session, validated)
            response = ReadOnlySQLResponse(
                sql=validated.sql,
                tables=list(validated.tables),
                columns=result.columns,
                rows=result.rows,
                row_count=len(result.rows),
                explain=result.explain,
                execution_user=self.sql_executor.execution_user,
                timeout_ms=int(self.settings.sql_query_timeout_seconds * 1_000),
            )
        except Exception as exc:
            await self._finish_audit(audit, error=exc)
            raise
        await self._finish_audit(audit, output=response.model_dump(mode="json"))
        return response

    async def forecast_sales(
        self,
        request: SalesForecastToolRequest,
    ) -> SalesForecastToolResponse:
        """调用与 HTTP Agent 相同的动态日期预测和确定性补货服务。"""
        audit = await self._start_audit(
            "forecast_sales",
            request.model_dump(mode="json"),
        )
        try:
            answer = await self.forecast_service.answer(
                request.question,
                forecast_days=request.forecast_days,
                forecast_start_date=request.forecast_start_date,
                forecast_end_date=request.forecast_end_date,
            )
            response = SalesForecastToolResponse(
                answer=answer.answer,
                forecast=answer.forecast,
                model=answer.model,
                usage=answer.usage,
            )
        except Exception as exc:
            await self._finish_audit(audit, error=exc)
            raise
        await self._finish_audit(audit, output=response.model_dump(mode="json"))
        return response


@lru_cache(maxsize=1)
def get_tool_service() -> InsightAgentToolService:
    """返回进程级工具服务，复用连接池与预测客户端。"""
    return InsightAgentToolService(audit_store=ToolCallStore())
