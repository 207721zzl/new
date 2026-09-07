"""只读执行器与 Text2SQL 最小闭环服务测试。"""

import asyncio
from decimal import Decimal

import pytest

from app.analysis.executor import ReadOnlySQLExecutor
from app.analysis.models import (
    QueryExecutionResult,
    SQLGenerationResult,
    Text2SQLCompletion,
    ValidatedSQL,
)
from app.analysis.service import DataAnalysisService
from app.config import Settings
from app.db.models import MetricDefinition
from app.errors import ConfigurationError, SQLExecutionError, UnsafeSQLError


class FakeMappings:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def fetchmany(self, size):
        return self.rows[:size]


class FakeResult:
    def __init__(self, rows, columns):
        self.rows = rows
        self.columns = columns

    def mappings(self):
        return FakeMappings(self.rows)

    def keys(self):
        return self.columns


class FakeExecutorSession:
    def __init__(self):
        self.calls = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params))
        if sql.startswith("SET SESSION"):
            return FakeResult([], [])
        if sql.startswith("EXPLAIN"):
            return FakeResult(
                [{"table": "p", "type": "range", "rows": 7}],
                ["table", "type", "rows"],
            )
        return FakeResult([{"gmv": Decimal("22682.00")}], ["gmv"])


def test_executor_runs_explain_and_select_with_five_second_mysql_timeout():
    settings = Settings(_env_file=None, sql_query_timeout_seconds=5)
    executor = ReadOnlySQLExecutor(settings)
    session = FakeExecutorSession()
    query = ValidatedSQL(
        sql="SELECT SUM(p.paid_amount) AS gmv FROM payments AS p LIMIT 1000",
        tables=("payments",),
        limit=1000,
    )

    result = asyncio.run(executor.execute(session, query))

    assert session.calls[0] == (
        "SET SESSION MAX_EXECUTION_TIME = :timeout_ms",
        {"timeout_ms": 5000},
    )
    assert session.calls[1][0].startswith("EXPLAIN SELECT")
    assert session.calls[2][0] == query.sql
    assert result.rows == [{"gmv": 22682.0}]
    assert result.explain[0]["type"] == "range"
    assert executor.execution_user == "insight_reader"


def test_executor_rejects_a_non_reader_database_url():
    settings = Settings(
        _env_file=None,
        database_read_url="mysql+asyncmy://admin:secret@localhost/insight_agent",
    )

    try:
        ReadOnlySQLExecutor(settings)
    except ConfigurationError as exc:
        assert "expected reader" in str(exc)
    else:
        raise AssertionError("non-reader URL must be rejected")


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


class FakeGenerator:
    async def generate(
        self,
        question,
        metrics,
        *,
        analysis_type="metric",
        repair_context=None,
    ):
        assert question == "上周华东区 GMV 是多少？"
        assert metrics[0].metric_code == "gmv"
        assert analysis_type == "metric"
        assert repair_context is None
        return Text2SQLCompletion(
            result=SQLGenerationResult(
                sql=(
                    "SELECT COALESCE(SUM(p.paid_amount), 0) AS gmv "
                    "FROM payments AS p JOIN orders AS o ON o.id = p.order_id "
                    "WHERE p.payment_status = 'success' AND o.region = '华东'"
                ),
                metric_code="gmv",
                value_column="gmv",
                assumptions=["上周按完整自然周计算"],
            ),
            model="deepseek-v4-pro",
            usage={"total_tokens": 100},
        )


class FakeReadOnlyExecutor:
    execution_user = "insight_reader"

    async def execute(self, session, query):
        assert query.sql.endswith("LIMIT 1000")
        assert query.tables == ("orders", "payments")
        return QueryExecutionResult(
            columns=["gmv"],
            rows=[{"gmv": 22682.0}],
            explain=[{"table": "p", "type": "range", "rows": 7}],
        )


def make_metric() -> MetricDefinition:
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


def test_gmv_question_completes_number_sql_and_evidence_loop():
    metrics = [make_metric()]
    settings = Settings(_env_file=None)
    service = DataAnalysisService(
        settings,
        generator=FakeGenerator(),
        executor=FakeReadOnlyExecutor(),
        session_factory=lambda: FakeRepositorySession(metrics),
    )

    result = asyncio.run(service.answer("上周华东区 GMV 是多少？"))

    assert result.analysis.value == 22682.0
    assert result.analysis.unit == "元"
    assert result.analysis.sql.endswith("LIMIT 1000")
    assert result.analysis.evidence.execution_user == "insight_reader"
    assert result.analysis.evidence.timeout_ms == 5000
    assert result.analysis.evidence.metric.formula.startswith("SUM")
    assert result.analysis.evidence.rows == [{"gmv": 22682.0}]
    assert result.answer == "支付 GMV 为 22,682.00 元。"


class RepairingGenerator:
    def __init__(self, *, always_unsafe=False):
        self.always_unsafe = always_unsafe
        self.repair_contexts = []

    async def generate(
        self,
        question,
        metrics,
        *,
        analysis_type="metric",
        repair_context=None,
    ):
        self.repair_contexts.append(repair_context)
        if self.always_unsafe or len(self.repair_contexts) == 1:
            sql = "DELETE FROM orders"
            usage = {"total_tokens": 40}
        else:
            sql = (
                "SELECT COALESCE(SUM(p.paid_amount), 0) AS gmv "
                "FROM payments AS p JOIN orders AS o ON o.id = p.order_id "
                "WHERE p.payment_status = 'success' AND o.region = '华东'"
            )
            usage = {"total_tokens": 60}
        return Text2SQLCompletion(
            result=SQLGenerationResult(
                sql=sql,
                metric_code="gmv",
                value_column="gmv",
                assumptions=["上周按完整自然周计算"],
            ),
            model="deepseek-v4-pro",
            usage=usage,
        )


def test_unsafe_sql_is_repaired_once_and_usage_is_aggregated():
    generator = RepairingGenerator()
    service = DataAnalysisService(
        Settings(_env_file=None, text2sql_max_repairs=2),
        generator=generator,
        executor=FakeReadOnlyExecutor(),
        session_factory=lambda: FakeRepositorySession([make_metric()]),
    )

    result = asyncio.run(service.answer("上周华东区 GMV 是多少？"))

    assert result.analysis.repair_count == 1
    assert result.usage == {"total_tokens": 100}
    assert result.analysis.evidence.assumptions[-1] == "Text2SQL 自动修复 1 次"
    assert generator.repair_contexts[0] is None
    assert generator.repair_contexts[1].attempt == 1
    assert generator.repair_contexts[1].failure_type == "unsafe_sql"
    assert generator.repair_contexts[1].previous_sql == "DELETE FROM orders"


def test_text2sql_repair_stops_after_configured_two_repairs():
    generator = RepairingGenerator(always_unsafe=True)
    service = DataAnalysisService(
        Settings(_env_file=None, text2sql_max_repairs=2),
        generator=generator,
        executor=FakeReadOnlyExecutor(),
        session_factory=lambda: FakeRepositorySession([make_metric()]),
    )

    with pytest.raises(UnsafeSQLError):
        asyncio.run(service.answer("上周华东区 GMV 是多少？"))

    assert len(generator.repair_contexts) == 3
    assert generator.repair_contexts[1].attempt == 1
    assert generator.repair_contexts[2].attempt == 2


class SafeRecordingGenerator:
    def __init__(self):
        self.repair_contexts = []

    async def generate(
        self,
        question,
        metrics,
        *,
        analysis_type="metric",
        repair_context=None,
    ):
        self.repair_contexts.append(repair_context)
        return Text2SQLCompletion(
            result=SQLGenerationResult(
                sql=(
                    "SELECT COALESCE(SUM(p.paid_amount), 0) AS gmv "
                    "FROM payments AS p JOIN orders AS o ON o.id = p.order_id"
                ),
                metric_code="gmv",
                value_column="gmv",
            ),
            model="deepseek-v4-pro",
            usage={"total_tokens": 25},
        )


class ExecutionFailsOnce:
    execution_user = "insight_reader"

    def __init__(self):
        self.calls = 0

    async def execute(self, session, query):
        self.calls += 1
        if self.calls == 1:
            raise SQLExecutionError("private database error")
        return QueryExecutionResult(
            columns=["gmv"],
            rows=[{"gmv": 22682.0}],
            explain=[],
        )


def test_execution_failure_gets_stable_repair_feedback_and_then_succeeds():
    generator = SafeRecordingGenerator()
    executor = ExecutionFailsOnce()
    service = DataAnalysisService(
        Settings(_env_file=None, text2sql_max_repairs=2),
        generator=generator,
        executor=executor,
        session_factory=lambda: FakeRepositorySession([make_metric()]),
    )

    result = asyncio.run(service.answer("上周华东区 GMV 是多少？"))

    assert result.analysis.repair_count == 1
    assert executor.calls == 2
    assert result.usage == {"total_tokens": 50}
    context = generator.repair_contexts[1]
    assert context.failure_type == "execution_error"
    assert "private database error" not in context.instruction
