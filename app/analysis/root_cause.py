"""确定性的经营指标原因分析与多维贡献拆解。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.executor import ReadOnlySQLExecutor
from app.analysis.models import (
    ComparisonAnalysis,
    DataAnalysisAnswer,
    DataAnalysisResult,
    MetricEvidence,
    QueryEvidence,
    RootCauseAnalysis,
    RootCauseBreakdown,
    RootCauseDimension,
    RootCauseItem,
)
from app.analysis.safety import SQLSafetyValidator
from app.config import Settings, get_settings
from app.db.models import MetricDefinition, Order, Product
from app.db.repositories import MetricDefinitionRepository
from app.db.session import get_read_session_factory
from app.errors import SQLExecutionError, Text2SQLGenerationError
from app.logging_config import get_logger


logger = get_logger("analysis.root_cause")


DIMENSION_COLUMNS: dict[RootCauseDimension, tuple[str, str]] = {
    "region": ("f.region", "区域"),
    "category": ("f.category", "品类"),
    "product": ("f.product_name", "商品"),
}
SUPPORTED_METRICS = frozenset({"gmv", "paid_order_count", "refund_amount_rate"})


@dataclass(frozen=True, slots=True)
class AnalysisPeriods:
    """原因分析使用的两个左闭右开业务周期。"""

    current_start: datetime
    current_end: datetime
    previous_start: datetime
    previous_end: datetime
    current_label: str
    previous_label: str
    assumption: str


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _shift_year(value: date, years: int) -> date:
    """跨闰年移动日期，并把 2 月 29 日稳定回退到 2 月 28 日。"""
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def resolve_periods(question: str, today: date) -> AnalysisPeriods:
    """从有限且可解释的周期表达中解析双周期边界。"""
    if "上月" in question or "上个月" in question:
        current_end_date = _month_start(today)
        current_start_date = _month_start(current_end_date - timedelta(days=1))
        if "同比" in question:
            previous_start_date = _shift_year(current_start_date, -1)
            previous_end_date = _shift_year(current_end_date, -1)
            assumption = "上月与去年同月比较，周期均为完整自然月"
        else:
            previous_end_date = current_start_date
            previous_start_date = _month_start(previous_end_date - timedelta(days=1))
            assumption = "上月与前一完整自然月比较"
    else:
        current_week_start = today - timedelta(days=today.weekday())
        current_end_date = current_week_start
        current_start_date = current_end_date - timedelta(days=7)
        if "同比" in question:
            previous_start_date = _shift_year(current_start_date, -1)
            previous_end_date = _shift_year(current_end_date, -1)
            assumption = "上周与去年同期完整自然周比较"
        else:
            previous_end_date = current_start_date
            previous_start_date = previous_end_date - timedelta(days=7)
            assumption = "上周与前一完整自然周比较"

    return AnalysisPeriods(
        current_start=datetime.combine(current_start_date, time.min),
        current_end=datetime.combine(current_end_date, time.min),
        previous_start=datetime.combine(previous_start_date, time.min),
        previous_end=datetime.combine(previous_end_date, time.min),
        current_label=f"{current_start_date.isoformat()} 至 {current_end_date.isoformat()}",
        previous_label=(
            f"{previous_start_date.isoformat()} 至 {previous_end_date.isoformat()}"
        ),
        assumption=assumption,
    )


def detect_metric_code(question: str) -> str:
    """以明确业务词识别当前可做贡献拆解的指标。"""
    normalized = question.lower()
    if "退款" in question:
        return "refund_amount_rate"
    if any(word in question for word in ("订单量", "订单数", "支付订单")):
        return "paid_order_count"
    if any(word in normalized for word in ("gmv", "销售额", "成交额", "实付金额")):
        return "gmv"
    raise Text2SQLGenerationError("原因分析暂不支持该指标")


def _sql_literal(value: str) -> str:
    """转义来自数据库维表而非直接来自用户输入的筛选值。"""
    return "'" + value.replace("'", "''") + "'"


def _timestamp_literal(value: datetime) -> str:
    return _sql_literal(value.strftime("%Y-%m-%d %H:%M:%S"))


def _fact_cte() -> str:
    """构造可加总的订单商品事实层，支付与退款按明细金额占比分摊。"""
    return """WITH item_totals AS (
  SELECT oi.order_id AS order_id, SUM(oi.line_amount) AS order_line_amount
  FROM order_items AS oi
  GROUP BY oi.order_id
), refund_totals AS (
  SELECT r.order_id AS order_id, SUM(r.refund_amount) AS refund_amount
  FROM refunds AS r
  WHERE r.refund_status = 'success'
  GROUP BY r.order_id
), fact AS (
  SELECT
    o.id AS order_id,
    o.region AS region,
    pr.category AS category,
    pr.name AS product_name,
    p.paid_at AS paid_at,
    p.paid_amount * oi.line_amount / NULLIF(it.order_line_amount, 0) AS paid_value,
    oi.line_amount / NULLIF(it.order_line_amount, 0) AS order_share,
    COALESCE(rt.refund_amount, 0) * oi.line_amount
      / NULLIF(it.order_line_amount, 0) AS refund_value
  FROM payments AS p
  JOIN orders AS o ON o.id = p.order_id
  JOIN order_items AS oi ON oi.order_id = o.id
  JOIN item_totals AS it ON it.order_id = o.id
  JOIN products AS pr ON pr.id = oi.product_id
  LEFT JOIN refund_totals AS rt ON rt.order_id = o.id
  WHERE p.payment_status = 'success'
)"""


def _period_condition(alias: str, start: datetime, end: datetime) -> str:
    return (
        f"{alias}.paid_at >= {_timestamp_literal(start)} "
        f"AND {alias}.paid_at < {_timestamp_literal(end)}"
    )


def _scope_condition(scope: dict[RootCauseDimension, str], alias: str = "f") -> str:
    field_names = {
        "region": "region",
        "category": "category",
        "product": "product_name",
    }
    return " AND ".join(
        f"{alias}.{field_names[dimension]} = {_sql_literal(value)}"
        for dimension, value in scope.items()
    )


def _window_where(periods: AnalysisPeriods, scope: dict[RootCauseDimension, str]) -> str:
    conditions = [
        f"f.paid_at >= {_timestamp_literal(periods.previous_start)}",
        f"f.paid_at < {_timestamp_literal(periods.current_end)}",
    ]
    if scope:
        conditions.append(_scope_condition(scope))
    return " AND ".join(conditions)


def _additive_expression(
    metric_code: str,
    periods: AnalysisPeriods,
    *,
    current: bool,
) -> str:
    start = periods.current_start if current else periods.previous_start
    end = periods.current_end if current else periods.previous_end
    source = "f.paid_value" if metric_code == "gmv" else "f.order_share"
    condition = _period_condition("f", start, end)
    return (
        "ROUND(COALESCE(SUM(CASE WHEN "
        f"{condition} THEN {source} ELSE 0 END), 0), 4)"
    )


def _refund_rate_expression(
    periods: AnalysisPeriods,
    *,
    current: bool,
) -> str:
    start = periods.current_start if current else periods.previous_start
    end = periods.current_end if current else periods.previous_end
    condition = _period_condition("f", start, end)
    return (
        "COALESCE(ROUND(100 * SUM(CASE WHEN "
        f"{condition} THEN f.refund_value ELSE 0 END) / NULLIF(SUM(CASE WHEN "
        f"{condition} THEN f.paid_value ELSE 0 END), 0), 4), 0)"
    )


def build_total_sql(
    metric_code: str,
    periods: AnalysisPeriods,
    scope: dict[RootCauseDimension, str],
) -> str:
    """生成双周期总体查询。"""
    if metric_code == "refund_amount_rate":
        current_expression = _refund_rate_expression(periods, current=True)
        previous_expression = _refund_rate_expression(periods, current=False)
    else:
        current_expression = _additive_expression(
            metric_code, periods, current=True
        )
        previous_expression = _additive_expression(
            metric_code, periods, current=False
        )
    return (
        f"{_fact_cte()}\n"
        f"SELECT {current_expression} AS current_value, "
        f"{previous_expression} AS previous_value\n"
        f"FROM fact AS f\nWHERE {_window_where(periods, scope)}"
    )


def build_breakdown_sql(
    metric_code: str,
    dimension: RootCauseDimension,
    periods: AnalysisPeriods,
    scope: dict[RootCauseDimension, str],
) -> str:
    """生成一个维度的可加总贡献查询。"""
    dimension_column, _ = DIMENSION_COLUMNS[dimension]
    where = _window_where(periods, scope)
    if metric_code != "refund_amount_rate":
        current_expression = _additive_expression(
            metric_code, periods, current=True
        )
        previous_expression = _additive_expression(
            metric_code, periods, current=False
        )
        return (
            f"{_fact_cte()}\n"
            f"SELECT {dimension_column} AS dimension_value, "
            f"{current_expression} AS current_value, "
            f"{previous_expression} AS previous_value\n"
            f"FROM fact AS f\nWHERE {where}\nGROUP BY {dimension_column}"
        )

    current_condition = _period_condition(
        "f", periods.current_start, periods.current_end
    )
    previous_condition = _period_condition(
        "f", periods.previous_start, periods.previous_end
    )
    totals = (
        "totals AS (\n"
        "  SELECT\n"
        f"    SUM(CASE WHEN {current_condition} THEN f.paid_value ELSE 0 END) "
        "AS current_paid,\n"
        f"    SUM(CASE WHEN {previous_condition} THEN f.paid_value ELSE 0 END) "
        "AS previous_paid\n"
        f"  FROM fact AS f\n  WHERE {where}\n)"
    )
    current_expression = (
        "COALESCE(ROUND(100 * SUM(CASE WHEN "
        f"{current_condition} THEN f.refund_value ELSE 0 END) "
        "/ NULLIF(MAX(t.current_paid), 0), 4), 0)"
    )
    previous_expression = (
        "COALESCE(ROUND(100 * SUM(CASE WHEN "
        f"{previous_condition} THEN f.refund_value ELSE 0 END) "
        "/ NULLIF(MAX(t.previous_paid), 0), 4), 0)"
    )
    return (
        f"{_fact_cte()}, {totals}\n"
        f"SELECT {dimension_column} AS dimension_value, "
        f"{current_expression} AS current_value, "
        f"{previous_expression} AS previous_value\n"
        "FROM fact AS f CROSS JOIN totals AS t\n"
        f"WHERE {where}\nGROUP BY {dimension_column}"
    )


class RootCauseAnalysisService:
    """执行基线和多维查询，并以查询结果确定性计算原因贡献。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        validator: SQLSafetyValidator | None = None,
        executor: ReadOnlySQLExecutor | None = None,
        session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
        today_provider: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.validator = validator or SQLSafetyValidator(self.settings.sql_query_limit)
        self.executor = executor or ReadOnlySQLExecutor(self.settings)
        self.session_factory = session_factory or get_read_session_factory()
        self.today_provider = today_provider or (
            lambda: datetime.now(ZoneInfo(self.settings.business_timezone)).date()
        )

    async def answer(self, question: str) -> DataAnalysisAnswer:
        """完成指标识别、范围解析、双周期查询、贡献计算和事实校验。"""
        metric_code = detect_metric_code(question)
        if metric_code not in SUPPORTED_METRICS:
            raise Text2SQLGenerationError(
                "原因分析暂不支持该指标",
                repairable=False,
            )
        periods = resolve_periods(question, self.today_provider())

        async with self.session_factory() as session:
            metric = await MetricDefinitionRepository(session).get_by_code(metric_code)
            if metric is None:
                raise Text2SQLGenerationError(
                    "原因分析指标未启用",
                    repairable=False,
                )
            scope = await self._resolve_scope(session, question)

            total_sql = build_total_sql(metric_code, periods, scope)
            total_query = self.validator.validate(total_sql)
            total_execution = await self.executor.execute(session, total_query)
            current_value, previous_value = self._read_pair(total_execution.rows)
            comparison = self._comparison(current_value, previous_value, periods)
            metric_evidence = self._metric_evidence(metric)
            total_evidence = self._query_evidence(
                metric_evidence, total_query, total_execution, [periods.assumption]
            )

            breakdowns: list[RootCauseBreakdown] = []
            for dimension in DIMENSION_COLUMNS:
                sql = build_breakdown_sql(metric_code, dimension, periods, scope)
                query = self.validator.validate(sql)
                execution = await self.executor.execute(session, query)
                items = self._build_items(
                    dimension,
                    execution.rows,
                    comparison.change,
                )
                current_error = round(
                    sum(float(item.current_value) for item in items)
                    - float(current_value),
                    4,
                )
                previous_error = round(
                    sum(float(item.previous_value) for item in items)
                    - float(previous_value),
                    4,
                )
                reconciled = self._is_reconciled(
                    current_error,
                    previous_error,
                    current_value,
                    previous_value,
                )
                breakdowns.append(
                    RootCauseBreakdown(
                        dimension=dimension,
                        label=DIMENSION_COLUMNS[dimension][1],
                        sql=query.sql,
                        evidence=self._query_evidence(
                            metric_evidence,
                            query,
                            execution,
                            [periods.assumption, "支付和退款按订单明细金额占比分摊"],
                        ),
                        items=items,
                        reconciled=reconciled,
                        current_reconciliation_error=current_error,
                        previous_reconciliation_error=previous_error,
                    )
                )

        failed_dimensions = [item.label for item in breakdowns if not item.reconciled]
        if failed_dimensions:
            raise SQLExecutionError(
                "原因分析维度未通过总额对账：" + "、".join(failed_dimensions)
            )

        primary_drivers = self._primary_drivers(breakdowns, scope)
        anomaly_count = sum(
            item.is_anomaly
            for breakdown in breakdowns
            for item in breakdown.items
        )
        limitations = [
            "贡献率按维度项变动额除以总体变动额计算；抵消项为负，绝对值可能超过 100%。",
            "异常项定义为与总体同向、贡献率至少 10%，且变化率至少 20% 或对比期为 0。",
            "退款金额率按支付订单归属周期分析，支付与退款按订单明细金额占比分摊。",
        ]
        root_cause = RootCauseAnalysis(
            scope=[f"{DIMENSION_COLUMNS[key][1]}={value}" for key, value in scope.items()],
            breakdowns=breakdowns,
            primary_drivers=primary_drivers,
            anomaly_count=anomaly_count,
            limitations=limitations,
        )
        analysis = DataAnalysisResult(
            value=current_value,
            unit=metric.unit,
            sql=total_query.sql,
            evidence=total_evidence,
            comparison=comparison,
            root_cause=root_cause,
        )
        answer = self._format_answer(metric, comparison, primary_drivers, anomaly_count)
        logger.info(
            "root cause analysis completed metric=%s scope=%s anomalies=%s",
            metric_code,
            ",".join(root_cause.scope) or "all",
            anomaly_count,
        )
        return DataAnalysisAnswer(
            answer=answer,
            analysis=analysis,
            model=None,
            usage=None,
        )

    @staticmethod
    async def _resolve_scope(
        session: AsyncSession,
        question: str,
    ) -> dict[RootCauseDimension, str]:
        scope: dict[RootCauseDimension, str] = {}
        region_result = await session.execute(select(Order.region).distinct())
        regions = sorted(
            {str(row[0]) for row in region_result.all() if row[0]},
            key=len,
            reverse=True,
        )
        product_result = await session.execute(select(Product.category, Product.name))
        product_rows = product_result.all()
        categories = sorted(
            {str(row[0]) for row in product_rows if row[0]},
            key=len,
            reverse=True,
        )
        products = sorted(
            {str(row[1]) for row in product_rows if row[1]},
            key=len,
            reverse=True,
        )
        for value in regions:
            if value in question:
                scope["region"] = value
                break
        for value in categories:
            if value in question:
                scope["category"] = value
                break
        for value in products:
            if value in question:
                scope["product"] = value
                break
        return scope

    @staticmethod
    def _read_pair(rows: list[dict[str, Any]]) -> tuple[int | float, int | float]:
        if not rows:
            raise SQLExecutionError("原因分析总体查询没有返回数据")
        row = rows[0]
        return (
            RootCauseAnalysisService._number(row.get("current_value")),
            RootCauseAnalysisService._number(row.get("previous_value")),
        )

    @staticmethod
    def _number(value: Any) -> int | float:
        if value is None:
            return 0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SQLExecutionError("原因分析查询返回了非数值字段")
        if isinstance(value, int):
            return value
        return round(float(value), 4)

    @staticmethod
    def _comparison(
        current: int | float,
        previous: int | float,
        periods: AnalysisPeriods,
    ) -> ComparisonAnalysis:
        change = round(float(current) - float(previous), 4)
        direction = "increase" if change > 0 else "decrease" if change < 0 else "flat"
        change_rate = None
        if previous != 0:
            change_rate = round(change / abs(float(previous)) * 100, 4)
        return ComparisonAnalysis(
            current_value=current,
            previous_value=previous,
            change=change,
            change_rate=change_rate,
            direction=direction,
            current_period=periods.current_label,
            previous_period=periods.previous_label,
        )

    @classmethod
    def _build_items(
        cls,
        dimension: RootCauseDimension,
        rows: list[dict[str, Any]],
        total_change: int | float,
    ) -> list[RootCauseItem]:
        items: list[RootCauseItem] = []
        for row in rows:
            name = str(row.get("dimension_value") or "未分类")
            current = cls._number(row.get("current_value"))
            previous = cls._number(row.get("previous_value"))
            change = round(float(current) - float(previous), 4)
            direction = "increase" if change > 0 else "decrease" if change < 0 else "flat"
            change_rate = None
            if previous != 0:
                change_rate = round(change / abs(float(previous)) * 100, 4)
            contribution_rate = None
            if total_change != 0:
                contribution_rate = round(change / float(total_change) * 100, 4)
            is_driver = change != 0 and change * float(total_change) > 0
            is_anomaly = bool(
                is_driver
                and contribution_rate is not None
                and contribution_rate >= 10
                and (previous == 0 or (change_rate is not None and abs(change_rate) >= 20))
            )
            items.append(
                RootCauseItem(
                    dimension=dimension,
                    name=name,
                    current_value=current,
                    previous_value=previous,
                    change=change,
                    change_rate=change_rate,
                    contribution_rate=contribution_rate,
                    direction=direction,
                    is_driver=is_driver,
                    is_anomaly=is_anomaly,
                )
            )
        items.sort(
            key=lambda item: (
                not item.is_driver,
                -(item.contribution_rate or 0),
                -abs(float(item.change)),
                item.name,
            )
        )
        return items

    @staticmethod
    def _is_reconciled(
        current_error: float,
        previous_error: float,
        current_value: int | float,
        previous_value: int | float,
    ) -> bool:
        current_tolerance = max(0.01, abs(float(current_value)) * 0.001)
        previous_tolerance = max(0.01, abs(float(previous_value)) * 0.001)
        return (
            abs(current_error) <= current_tolerance
            and abs(previous_error) <= previous_tolerance
        )

    @staticmethod
    def _primary_drivers(
        breakdowns: list[RootCauseBreakdown],
        scope: dict[RootCauseDimension, str],
    ) -> list[RootCauseItem]:
        drivers: list[RootCauseItem] = []
        for breakdown in breakdowns:
            if breakdown.dimension in scope:
                continue
            driver = next((item for item in breakdown.items if item.is_driver), None)
            if driver is not None:
                drivers.append(driver)
        if not drivers:
            for breakdown in breakdowns:
                driver = next((item for item in breakdown.items if item.is_driver), None)
                if driver is not None:
                    drivers.append(driver)
        return drivers[:3]

    def _query_evidence(self, metric, query, execution, assumptions) -> QueryEvidence:
        return QueryEvidence(
            metric=metric,
            columns=execution.columns,
            rows=execution.rows[: self.settings.sql_evidence_row_limit],
            row_count=len(execution.rows),
            explain=execution.explain,
            tables=list(query.tables),
            execution_user=self.executor.execution_user,
            timeout_ms=int(self.settings.sql_query_timeout_seconds * 1000),
            assumptions=assumptions,
        )

    @staticmethod
    def _metric_evidence(metric: MetricDefinition) -> MetricEvidence:
        return MetricEvidence(
            code=metric.metric_code,
            name=metric.metric_name,
            description=metric.description,
            formula=metric.formula,
            unit=metric.unit,
            version=metric.version,
        )

    @staticmethod
    def _format_number(value: int | float) -> str:
        if isinstance(value, int):
            return f"{value:,}"
        return f"{value:,.2f}"

    @classmethod
    def _format_answer(
        cls,
        metric: MetricDefinition,
        comparison: ComparisonAnalysis,
        drivers: list[RootCauseItem],
        anomaly_count: int,
    ) -> str:
        movement = {
            "increase": "增加",
            "decrease": "减少",
            "flat": "持平",
        }[comparison.direction]
        rate = (
            "变动率不适用"
            if comparison.change_rate is None
            else f"变动率 {comparison.change_rate:+.2f}%"
        )
        answer = (
            f"{metric.metric_name}当前周期为 {cls._format_number(comparison.current_value)} "
            f"{metric.unit}，对比周期为 {cls._format_number(comparison.previous_value)} "
            f"{metric.unit}，{movement} {cls._format_number(abs(comparison.change))} "
            f"{metric.unit}，{rate}。"
        )
        if drivers:
            driver_text = []
            for item in drivers:
                label = DIMENSION_COLUMNS[item.dimension][1]
                contribution = (
                    "贡献率不适用"
                    if item.contribution_rate is None
                    else f"贡献 {item.contribution_rate:.2f}%"
                )
                driver_text.append(
                    f"{label}「{item.name}」变动 "
                    f"{cls._format_number(item.change)} {metric.unit}（{contribution}）"
                )
            answer += " 主要驱动：" + "；".join(driver_text) + "。"
        answer += f" 共识别 {anomaly_count} 个异常维度项，所有数字已与总体查询完成对账。"
        return answer


@lru_cache(maxsize=1)
def get_root_cause_analysis_service() -> RootCauseAnalysisService:
    """返回进程级原因分析服务。"""
    return RootCauseAnalysisService()
