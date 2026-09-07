"""FastMCP 工具业务层的契约与安全复用测试。"""

import asyncio
from datetime import date

import pytest

from app.analysis.models import QueryExecutionResult
from app.config import Settings
from app.errors import UnsafeSQLError
from app.forecast.models import (
    ForecastPoint,
    ReplenishmentSuggestion,
    SalesForecastAnswer,
    SalesForecastResult,
)
from app.mcp.models import (
    ReadOnlySQLRequest,
    SalesForecastToolRequest,
    TableSchemaRequest,
)
from app.mcp.tools import InsightAgentToolService


class FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeSessionFactory:
    def __call__(self):
        return FakeSessionContext()


class FakeSQLExecutor:
    execution_user = "insight_reader"

    async def execute(self, session, query):
        assert session is not None
        assert query.tables == ("orders",)
        assert query.sql.endswith("LIMIT 1000")
        return QueryExecutionResult(
            columns=["region"],
            rows=[{"region": "华东"}],
            explain=[{"type": "ALL"}],
        )


class FakeForecastService:
    async def answer(self, question, **kwargs):
        assert question == "预测未来 7 天耳机销量"
        assert kwargs["forecast_days"] == 7
        forecast = SalesForecastResult(
            subject_type="category",
            subject_name="耳机",
            forecast_start_date=date(2026, 7, 13),
            forecast_end_date=date(2026, 7, 19),
            horizon_days=7,
            data_through=date(2026, 7, 12),
            points=[
                ForecastPoint(
                    date=date(2026, 7, 13),
                    predicted_quantity=5,
                    lower_bound=4,
                    upper_bound=6,
                )
            ],
            total_predicted_quantity=35,
            model_version="torch-global-test",
            generation_method="torch_global",
            prediction_device="cuda:test-gpu",
            explanation_model="deepseek-test",
            history_start_date=date(2026, 4, 14),
            history_days=90,
            historical_daily_average=4.5,
            reasoning_summary="历史销量平稳。",
            replenishment=ReplenishmentSuggestion(
                current_stock=20,
                forecast_demand=35,
                safety_stock=15,
                target_stock=50,
                recommended_quantity=30,
                safety_stock_days=3,
            ),
        )
        return SalesForecastAnswer(
            answer="未来 7 天预计销售 35 件。",
            forecast=forecast,
            model="torch-global-test",
            usage={"total_tokens": 42},
        )


def build_service() -> InsightAgentToolService:
    return InsightAgentToolService(
        Settings(_env_file=None, model_preload_enabled=False),
        session_factory=FakeSessionFactory(),
        sql_executor=FakeSQLExecutor(),
        forecast_service=FakeForecastService(),
    )


def test_schema_tool_returns_shared_allowlist_and_filters_one_table():
    service = build_service()

    all_tables = asyncio.run(service.get_table_schema(TableSchemaRequest()))
    orders = asyncio.run(
        service.get_table_schema(TableSchemaRequest(table_name="orders"))
    )

    assert "orders" in {table.name for table in all_tables.tables}
    assert [table.name for table in orders.tables] == ["orders"]
    assert "region" in {column.name for column in orders.tables[0].columns}


def test_schema_tool_rejects_non_allowlisted_table():
    with pytest.raises(ValueError, match="not allowlisted"):
        asyncio.run(
            build_service().get_table_schema(
                TableSchemaRequest(table_name="mysql.user")
            )
        )


def test_readonly_sql_tool_reuses_validator_and_executor():
    response = asyncio.run(
        build_service().execute_readonly_sql(
            ReadOnlySQLRequest(sql="SELECT o.region FROM orders AS o")
        )
    )

    assert response.execution_user == "insight_reader"
    assert response.row_count == 1
    assert response.rows == [{"region": "华东"}]
    assert response.timeout_ms == 5000


def test_readonly_sql_tool_blocks_writes_before_opening_database():
    with pytest.raises(UnsafeSQLError):
        asyncio.run(
            build_service().execute_readonly_sql(
                ReadOnlySQLRequest(sql="DELETE FROM orders")
            )
        )


def test_forecast_tool_reuses_dynamic_date_contract_and_result_model():
    response = asyncio.run(
        build_service().forecast_sales(
            SalesForecastToolRequest(
                question="预测未来 7 天耳机销量",
                forecast_days=7,
            )
        )
    )

    assert response.forecast.horizon_days == 7
    assert response.forecast.replenishment.recommended_quantity == 30
    assert response.model == "torch-global-test"
    assert response.usage == {"total_tokens": 42}
