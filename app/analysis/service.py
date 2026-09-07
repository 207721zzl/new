"""Text2SQL 生成、安全校验、只读执行与证据聚合服务。"""

from decimal import Decimal
from functools import lru_cache
from numbers import Real
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.deepseek import Text2SQLClient
from app.analysis.executor import ReadOnlySQLExecutor
from app.analysis.models import (
    AnalysisType,
    ComparisonAnalysis,
    DataAnalysisAnswer,
    DataAnalysisAttemptResult,
    DataAnalysisResult,
    MetricEvidence,
    QueryEvidence,
    Text2SQLRepairContext,
)
from app.analysis.safety import SQLSafetyValidator
from app.analysis.semantics import validate_metric_semantics
from app.config import Settings, get_settings
from app.db.models import MetricDefinition
from app.db.repositories import MetricDefinitionRepository
from app.db.session import get_read_session_factory
from app.errors import SQLExecutionError, Text2SQLGenerationError, UnsafeSQLError
from app.logging_config import get_logger


logger = get_logger("analysis.service")


class DataAnalysisService:
    """完成“问题 → SQL → 校验 → 只读执行 → 可审计答案”的最小闭环。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        generator: Text2SQLClient | None = None,
        validator: SQLSafetyValidator | None = None,
        executor: ReadOnlySQLExecutor | None = None,
        session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.generator = generator or Text2SQLClient(self.settings)
        self.validator = validator or SQLSafetyValidator(self.settings.sql_query_limit)
        self.executor = executor or ReadOnlySQLExecutor(self.settings)
        self.session_factory = session_factory or get_read_session_factory()

    async def answer(
        self,
        question: str,
        *,
        analysis_type: AnalysisType = "metric",
    ) -> DataAnalysisAnswer:
        """执行完整数据分析链，并返回数值、最终 SQL 与数据证据。"""
        metrics = await self.load_metrics()
        repair_context = None
        repair_history: list[Text2SQLRepairContext] = []
        aggregate_usage: dict[str, int] | None = None
        for attempt in range(self.settings.text2sql_max_repairs + 1):
            completion = None
            try:
                completion = await self.generate_sql(
                    question,
                    metrics,
                    analysis_type=analysis_type,
                    repair_context=repair_context,
                )
                aggregate_usage = self.merge_usage(
                    aggregate_usage,
                    completion.usage,
                )
                attempt_result = await self.validate_and_execute(
                    question,
                    metrics,
                    completion,
                    analysis_type=analysis_type,
                )
                break
            except (Text2SQLGenerationError, UnsafeSQLError, SQLExecutionError) as exc:
                if (
                    isinstance(exc, Text2SQLGenerationError)
                    and not exc.repairable
                ):
                    raise
                if attempt >= self.settings.text2sql_max_repairs:
                    logger.warning(
                        "Text2SQL repair exhausted attempts=%s failure_type=%s",
                        attempt,
                        type(exc).__name__,
                    )
                    raise
                repair_context = self.build_repair_context(
                    attempt=attempt + 1,
                    exc=exc,
                    previous_sql=(
                        completion.result.sql if completion is not None else None
                    ),
                )
                repair_history.append(repair_context)
                logger.info(
                    "Text2SQL semantic repair scheduled attempt=%s failure_type=%s",
                    repair_context.attempt,
                    repair_context.failure_type,
                )
        else:  # pragma: no cover - loop either breaks or raises
            raise Text2SQLGenerationError("Text2SQL repair loop ended unexpectedly")

        return self.finalize_answer(
            attempt_result,
            repair_history=repair_history,
            aggregate_usage=aggregate_usage,
        )

    async def load_metrics(self) -> list[MetricDefinition]:
        """加载图运行与直接服务调用共享的活跃指标定义。"""
        return await self._load_metrics()

    async def generate_sql(
        self,
        question: str,
        metrics: list[MetricDefinition],
        *,
        analysis_type: AnalysisType,
        repair_context: Text2SQLRepairContext | None = None,
    ):
        """执行一次结构化 SQL 生成，修复循环由调用方编排。"""
        return await self.generator.generate(
            question,
            metrics,
            analysis_type=analysis_type,
            repair_context=repair_context,
        )

    async def validate_and_execute(
        self,
        question: str,
        metrics: list[MetricDefinition],
        completion,
        *,
        analysis_type: AnalysisType,
    ) -> DataAnalysisAttemptResult:
        """校验一次生成结果并执行只读 SQL，不在方法内部重试。"""
        metric = self._resolve_metric(completion, metrics, analysis_type)
        validate_metric_semantics(completion.result, metric, question)
        validated = self.validator.validate(completion.result.sql)
        async with self.session_factory() as session:
            execution = await self.executor.execute(session, validated)

        value = self._extract_value(
            execution.rows,
            completion.result.value_column,
        )
        comparison = None
        if analysis_type == "comparison":
            comparison = self._build_comparison(
                execution.rows,
                completion.result,
            )
            value = comparison.current_value
        return DataAnalysisAttemptResult(
            completion=completion,
            metric=metric,
            validated=validated,
            execution=execution,
            value=value,
            comparison=comparison,
        )

    def finalize_answer(
        self,
        attempt: DataAnalysisAttemptResult,
        *,
        repair_history: list[Text2SQLRepairContext],
        aggregate_usage: dict[str, int] | None,
    ) -> DataAnalysisAnswer:
        """把成功图状态确定性转换为公开数据分析结果。"""
        completion = attempt.completion
        metric = attempt.metric
        validated = attempt.validated
        execution = attempt.execution
        comparison = attempt.comparison
        assumptions = list(completion.result.assumptions)
        if repair_history:
            assumptions.append(f"Text2SQL 自动修复 {len(repair_history)} 次")
        evidence = QueryEvidence(
            metric=MetricEvidence(
                code=metric.metric_code,
                name=metric.metric_name,
                description=metric.description,
                formula=metric.formula,
                unit=metric.unit,
                version=metric.version,
            ),
            columns=execution.columns,
            rows=execution.rows[: self.settings.sql_evidence_row_limit],
            row_count=len(execution.rows),
            explain=execution.explain,
            tables=list(validated.tables),
            execution_user=self.executor.execution_user,
            timeout_ms=int(self.settings.sql_query_timeout_seconds * 1000),
            assumptions=assumptions,
        )
        analysis = DataAnalysisResult(
            value=attempt.value,
            unit=metric.unit,
            sql=validated.sql,
            evidence=evidence,
            comparison=comparison,
            repair_count=len(repair_history),
        )
        logger.info(
            "data analysis completed metric=%s tables=%s repairs=%s",
            metric.metric_code,
            ",".join(validated.tables),
            len(repair_history),
        )
        answer = (
            f"{metric.metric_name} 为 {self._format_value(attempt.value)} "
            f"{metric.unit}。"
        )
        if comparison is not None:
            answer = self._format_comparison_answer(metric, comparison)
        return DataAnalysisAnswer(
            answer=answer,
            analysis=analysis,
            model=completion.model,
            usage=aggregate_usage,
        )

    @classmethod
    def build_repair_context(
        cls,
        *,
        attempt: int,
        exc: Exception,
        previous_sql: str | None,
    ) -> Text2SQLRepairContext:
        """为 LangGraph 循环或直接调用构造脱敏的修复反馈。"""
        return cls._build_repair_context(
            attempt=attempt,
            exc=exc,
            previous_sql=previous_sql,
        )

    @staticmethod
    def build_repair_context_for_failure(
        *,
        attempt: int,
        failure_type: str,
        previous_sql: str | None,
    ) -> Text2SQLRepairContext:
        """根据可序列化图状态创建下一次 Text2SQL 修复上下文。"""
        if failure_type == "unsafe_sql":
            instruction = (
                "SQL 未通过只读安全校验。仅使用给定白名单表字段，返回单条 "
                "SELECT 或 WITH ... SELECT，不使用通配符、系统表、写操作或危险函数。"
            )
        elif failure_type == "execution_error":
            instruction = (
                "SQL 未能通过 EXPLAIN 或只读执行。核对 MySQL 语法、连接条件、"
                "聚合列别名和结果列，简化查询并保留相同指标口径。"
            )
        else:
            failure_type = "contract_error"
            instruction = (
                "响应未满足结构化契约。返回完整 JSON，分析类型、metric_code、"
                "数值列别名和周期字段必须与用户问题及当前路由一致；区域只使用"
                "华东/华南/华北，百分比指标乘以 100，退款金额率分别按 refunded_at "
                "和 paid_at 过滤同一统计周期；用户给出的单日或起止日期必须严格使用"
                "对应的左闭右开边界，不能扩展为整周或整月。"
            )
        return Text2SQLRepairContext(
            attempt=attempt,
            failure_type=failure_type,
            instruction=instruction,
            previous_sql=previous_sql,
        )

    @classmethod
    def merge_usage(
        cls,
        total: dict[str, int] | None,
        current: dict[str, int] | None,
    ) -> dict[str, int] | None:
        """合并多次生成的 Token 计数。"""
        return cls._merge_usage(total, current)

    async def _load_metrics(self) -> list[MetricDefinition]:
        async with self.session_factory() as session:
            metrics = await MetricDefinitionRepository(session).list_active()
        if not metrics:
            raise Text2SQLGenerationError(
                "no active metric definitions",
                repairable=False,
            )
        return metrics

    @staticmethod
    def _resolve_metric(completion, metrics, analysis_type) -> MetricDefinition:
        if completion.result.analysis_type != analysis_type:
            raise Text2SQLGenerationError(
                "generated analysis_type does not match routed intent"
            )
        metric = next(
            (
                item
                for item in metrics
                if item.metric_code == completion.result.metric_code
            ),
            None,
        )
        if metric is None:
            raise Text2SQLGenerationError("generated metric_code is not active")
        return metric

    @staticmethod
    def _build_repair_context(
        *,
        attempt: int,
        exc: Exception,
        previous_sql: str | None,
    ) -> Text2SQLRepairContext:
        failure_type = (
            "unsafe_sql"
            if isinstance(exc, UnsafeSQLError)
            else "execution_error"
            if isinstance(exc, SQLExecutionError)
            else "contract_error"
        )
        return DataAnalysisService.build_repair_context_for_failure(
            attempt=attempt,
            failure_type=failure_type,
            previous_sql=previous_sql,
        )

    @staticmethod
    def _merge_usage(
        total: dict[str, int] | None,
        current: dict[str, int] | None,
    ) -> dict[str, int] | None:
        if not current:
            return total
        merged = dict(total or {})
        for key, value in current.items():
            if isinstance(value, int):
                merged[key] = merged.get(key, 0) + value
        return merged or None

    @staticmethod
    def _extract_value(rows: list[dict[str, Any]], column: str) -> int | float:
        if not rows:
            raise SQLExecutionError("query returned no rows")
        if column not in rows[0]:
            raise SQLExecutionError("value_column is missing from query result")
        value = rows[0][column]
        if value is None:
            return 0
        if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
            raise SQLExecutionError("value_column is not numeric")
        if isinstance(value, int):
            return value
        return float(value)

    @staticmethod
    def _format_value(value: int | float) -> str:
        if isinstance(value, int):
            return f"{value:,}"
        return f"{value:,.2f}"

    def _build_comparison(self, rows, generation) -> ComparisonAnalysis:
        current = self._extract_value(rows, generation.current_value_column)
        previous = self._extract_value(rows, generation.previous_value_column)
        change = current - previous
        if isinstance(current, int) and isinstance(previous, int):
            normalized_change: int | float = change
        else:
            normalized_change = float(change)
        if change > 0:
            direction = "increase"
        elif change < 0:
            direction = "decrease"
        else:
            direction = "flat"
        change_rate = None
        if previous != 0:
            change_rate = round(float(change / abs(previous) * 100), 4)
        return ComparisonAnalysis(
            current_value=current,
            previous_value=previous,
            change=normalized_change,
            change_rate=change_rate,
            direction=direction,
            current_period=generation.current_period or "当前周期",
            previous_period=generation.previous_period or "对比周期",
        )

    def _format_comparison_answer(
        self,
        metric: MetricDefinition,
        comparison: ComparisonAnalysis,
    ) -> str:
        current = self._format_value(comparison.current_value)
        previous = self._format_value(comparison.previous_value)
        change = self._format_value(abs(comparison.change))
        if comparison.direction == "increase":
            movement = f"增加 {change} {metric.unit}"
        elif comparison.direction == "decrease":
            movement = f"减少 {change} {metric.unit}"
        else:
            movement = "保持不变"
        if comparison.change_rate is None:
            rate = "对比周期为 0，增长率不适用"
        else:
            rate = f"变动率 {comparison.change_rate:+.2f}%"
        return (
            f"{metric.metric_name} 当前周期为 {current} {metric.unit}，"
            f"对比周期为 {previous} {metric.unit}，{movement}，{rate}。"
        )


@lru_cache(maxsize=1)
def get_data_analysis_service() -> DataAnalysisService:
    """返回进程级数据分析服务。"""
    return DataAnalysisService()
