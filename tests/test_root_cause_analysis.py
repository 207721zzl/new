"""原因分析的路由、SQL 安全、贡献计算与事实对账测试。"""

import asyncio
from datetime import date
from types import SimpleNamespace

from app import graph
from app.analysis.models import QueryExecutionResult
from app.analysis.root_cause import (
    DIMENSION_COLUMNS,
    SUPPORTED_METRICS,
    RootCauseAnalysisService,
    build_breakdown_sql,
    build_total_sql,
    resolve_periods,
)
from app.analysis.safety import SQLSafetyValidator
from app.config import Settings
from app.db.models import MetricDefinition


class FakeRowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeRootCauseSession:
    async def scalar(self, statement):
        return MetricDefinition(
            id=1,
            metric_code="gmv",
            metric_name="支付 GMV",
            description="支付成功金额合计",
            formula="SUM(payments.paid_amount)",
            unit="元",
            time_grain="day",
            source_tables=["orders", "payments"],
            version="1.0",
            is_active=True,
        )

    async def execute(self, statement):
        sql = str(statement)
        if "orders.region" in sql:
            return FakeRowsResult([("华东",), ("华南",)])
        return FakeRowsResult([("手机", "旗舰手机"), ("耳机", "降噪耳机")])


class FakeSessionContext:
    async def __aenter__(self):
        return FakeRootCauseSession()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeRootCauseExecutor:
    def __init__(self):
        self.call_count = 0

    @property
    def execution_user(self):
        return "insight_reader"

    async def execute(self, session, query):
        self.call_count += 1
        rows_by_call = {
            1: [{"current_value": 100.0, "previous_value": 160.0}],
            2: [
                {
                    "dimension_value": "华东",
                    "current_value": 100.0,
                    "previous_value": 160.0,
                }
            ],
            3: [
                {
                    "dimension_value": "手机",
                    "current_value": 60.0,
                    "previous_value": 120.0,
                },
                {
                    "dimension_value": "耳机",
                    "current_value": 40.0,
                    "previous_value": 40.0,
                },
            ],
            4: [
                {
                    "dimension_value": "旗舰手机",
                    "current_value": 60.0,
                    "previous_value": 120.0,
                },
                {
                    "dimension_value": "降噪耳机",
                    "current_value": 40.0,
                    "previous_value": 40.0,
                },
            ],
        }
        rows = rows_by_call[self.call_count]
        return QueryExecutionResult(
            columns=list(rows[0]),
            rows=rows,
            explain=[{"table": "payments", "type": "range"}],
        )


def test_root_cause_sql_for_every_supported_metric_passes_safety_validation():
    periods = resolve_periods("华东地区上周 GMV 为什么下降？", date(2026, 7, 18))
    validator = SQLSafetyValidator()

    for metric_code in SUPPORTED_METRICS:
        total = validator.validate(build_total_sql(metric_code, periods, {}))
        assert total.limit == 1000
        assert "payments" in total.tables
        for dimension in DIMENSION_COLUMNS:
            breakdown = validator.validate(
                build_breakdown_sql(metric_code, dimension, periods, {})
            )
            assert breakdown.limit == 1000
            assert "dimension_value" in breakdown.sql


def test_root_cause_service_calculates_drivers_anomalies_and_reconciliation():
    executor = FakeRootCauseExecutor()
    service = RootCauseAnalysisService(
        Settings(_env_file=None),
        executor=executor,
        session_factory=lambda: FakeSessionContext(),
        today_provider=lambda: date(2026, 7, 18),
    )

    result = asyncio.run(service.answer("华东地区上周 GMV 为什么下降？"))

    assert result.model is None
    assert result.analysis.comparison.direction == "decrease"
    assert result.analysis.comparison.change == -60.0
    assert result.analysis.root_cause.scope == ["区域=华东"]
    assert all(
        breakdown.reconciled for breakdown in result.analysis.root_cause.breakdowns
    )
    assert result.analysis.root_cause.primary_drivers[0].name == "手机"
    assert result.analysis.root_cause.primary_drivers[0].contribution_rate == 100.0
    assert result.analysis.root_cause.anomaly_count == 3
    assert "所有数字已与总体查询完成对账" in result.answer
    assert executor.call_count == 4


def test_root_cause_items_mark_offsets_as_non_drivers():
    items = RootCauseAnalysisService._build_items(
        "category",
        [
            {
                "dimension_value": "手机",
                "current_value": 40.0,
                "previous_value": 100.0,
            },
            {
                "dimension_value": "耳机",
                "current_value": 60.0,
                "previous_value": 40.0,
            },
        ],
        total_change=-40.0,
    )

    assert items[0].name == "手机"
    assert items[0].is_driver is True
    assert items[0].contribution_rate == 150.0
    assert items[1].name == "耳机"
    assert items[1].is_driver is False
    assert items[1].contribution_rate == -50.0


class FakeRootCauseGraphService:
    async def answer(self, question):
        assert "为什么" in question
        return SimpleNamespace(
            answer="支付 GMV 下降，主要由手机品类驱动。",
            analysis=SimpleNamespace(
                model_dump=lambda mode: {
                    "value": 100.0,
                    "root_cause": {"anomaly_count": 2},
                }
            ),
            model=None,
            usage=None,
        )


def test_graph_routes_why_question_into_root_cause_node(monkeypatch):
    class FakeIntentClassifier:
        async def classify(self, question, history_messages=None):
            return SimpleNamespace(
                result=SimpleNamespace(
                    intent="root_cause_analysis",
                    confidence=0.99,
                    rewritten_question=question,
                    reason="测试归因意图",
                ),
                model="deepseek-intent-test",
                usage={"total_tokens": 20},
            )

    monkeypatch.setattr(
        graph,
        "get_intent_classifier",
        lambda: FakeIntentClassifier(),
    )
    monkeypatch.setattr(
        graph,
        "get_root_cause_analysis_service",
        lambda: FakeRootCauseGraphService(),
    )

    result = asyncio.run(
        graph.agent_graph.ainvoke(
            {
                "run_id": "root-cause-run",
                "question": "华东地区上周 GMV 为什么下降？",
            }
        )
    )

    assert result["intent"] == "root_cause_analysis"
    assert result["analysis"]["root_cause"]["anomaly_count"] == 2
    assert "手机品类" in result["answer"]
