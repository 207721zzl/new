"""同比环比意图、结构化查询和确定性数值计算测试。"""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.analysis.models import (
    QueryExecutionResult,
    SQLGenerationResult,
    Text2SQLCompletion,
)
from app.analysis.service import DataAnalysisService
from app.config import Settings
from app.db.models import MetricDefinition


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeScalarResult:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)


class FakeRepositorySession:
    def __init__(self, metrics):
        self.metrics = metrics

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def scalars(self, statement):
        return FakeScalarResult(self.metrics)


class FakeComparisonGenerator:
    async def generate(
        self,
        question,
        metrics,
        *,
        analysis_type="metric",
        repair_context=None,
    ):
        assert "相比" in question
        assert metrics[0].metric_code == "gmv"
        assert analysis_type == "comparison"
        assert repair_context is None
        return Text2SQLCompletion(
            result=SQLGenerationResult(
                sql=(
                    "SELECT 22682 AS current_value, 18000 AS previous_value "
                    "FROM payments AS p"
                ),
                metric_code="gmv",
                value_column="current_value",
                analysis_type="comparison",
                current_value_column="current_value",
                previous_value_column="previous_value",
                current_period="2026-07-06 至 2026-07-13",
                previous_period="2026-06-29 至 2026-07-06",
                assumptions=["按完整自然周比较"],
            ),
            model="deepseek-v4-pro",
            usage={"total_tokens": 120},
        )


class FakeComparisonExecutor:
    execution_user = "insight_reader"

    async def execute(self, session, query):
        assert query.sql.endswith("LIMIT 1000")
        return QueryExecutionResult(
            columns=["current_value", "previous_value"],
            rows=[{"current_value": 22682.0, "previous_value": 18000.0}],
            explain=[{"table": "p", "type": "range"}],
        )


def make_metric():
    return MetricDefinition(
        id=1,
        metric_code="gmv",
        metric_name="支付 GMV",
        description="支付成功订单的实付金额合计",
        formula="SUM(payments.paid_amount) WHERE payment_status = 'success'",
        unit="元",
        time_grain="day",
        source_tables=["orders", "payments"],
        version="1.0",
        is_active=True,
    )


def test_fifteen_golden_questions_define_comparison_expectations():
    cases = json.loads(
        (PROJECT_ROOT / "data/evaluation/comparison_cases.json").read_text(
            encoding="utf-8"
        )
    )

    assert len(cases) == 15
    assert {case["expected_intent"] for case in cases} == {"comparison_analysis"}


def test_comparison_contract_requires_both_numeric_columns():
    with pytest.raises(ValidationError):
        SQLGenerationResult(
            sql="SELECT 1 AS current_value FROM payments",
            metric_code="gmv",
            value_column="current_value",
            analysis_type="comparison",
            current_value_column="current_value",
        )


def test_comparison_service_calculates_change_rate_from_query_rows():
    service = DataAnalysisService(
        Settings(_env_file=None),
        generator=FakeComparisonGenerator(),
        executor=FakeComparisonExecutor(),
        session_factory=lambda: FakeRepositorySession([make_metric()]),
    )

    result = asyncio.run(
        service.answer(
            "上周华东区 GMV 与前一周相比变化如何？",
            analysis_type="comparison",
        )
    )

    comparison = result.analysis.comparison
    assert comparison is not None
    assert comparison.current_value == 22682.0
    assert comparison.previous_value == 18000.0
    assert comparison.change == 4682.0
    assert comparison.change_rate == 26.0111
    assert comparison.direction == "increase"
    assert "变动率 +26.01%" in result.answer
    assert result.analysis.evidence.rows[0]["previous_value"] == 18000.0


def test_comparison_with_zero_previous_value_has_no_change_rate():
    service = DataAnalysisService(Settings(_env_file=None))
    generation = SQLGenerationResult(
        sql="SELECT 10 AS current_value, 0 AS previous_value FROM payments",
        metric_code="gmv",
        value_column="current_value",
        analysis_type="comparison",
        current_value_column="current_value",
        previous_value_column="previous_value",
    )

    comparison = service._build_comparison(
        [{"current_value": 10, "previous_value": 0}],
        generation,
    )

    assert comparison.direction == "increase"
    assert comparison.change == 10
    assert comparison.change_rate is None
