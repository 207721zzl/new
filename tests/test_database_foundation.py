"""MySQL 数据模型、模拟数据、查询构造和健康检查测试。"""

import asyncio
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.dialects import mysql

from app.config import Settings
from app.db.base import Base
from app.db.health import check_mysql_connection
from app.db.repositories import build_gmv_query
from app.db.seed_data import build_seed_bundle
from app.db.models import MetricDefinition
from scripts.seed_mysql import upsert_rows


def test_metadata_contains_initial_business_tables():
    expected = {
        "products",
        "customers",
        "campaigns",
        "daily_traffic",
        "orders",
        "order_items",
        "payments",
        "refunds",
        "metric_definitions",
        "agent_runs",
        "documents",
        "document_parent_chunks",
        "document_chunks",
        "indexing_jobs",
        "conversations",
        "messages",
        "tool_calls",
        "user_feedback",
        "eval_cases",
        "users",
        "auth_sessions",
        "admin_audit_logs",
    }

    assert expected <= set(Base.metadata.tables)
    assert Base.metadata.tables["products"].c.sku.unique is True
    assert Base.metadata.tables["payments"].c.order_id.unique is True
    assert Base.metadata.tables["agent_runs"].c.run_id.primary_key is True
    assert "conversation_id" in Base.metadata.tables["agent_runs"].c
    assert "turn_index" in Base.metadata.tables["agent_runs"].c
    assert "run_id" not in Base.metadata.tables["conversations"].c
    assert "batch_id" in Base.metadata.tables["indexing_jobs"].c
    assert "original_filename" in Base.metadata.tables["indexing_jobs"].c
    assert "content_sha256" in Base.metadata.tables["indexing_jobs"].c
    assert Base.metadata.tables["conversations"].c.user_id.nullable is False
    assert Base.metadata.tables["indexing_jobs"].c.created_by_user_id.nullable is False
    assert Base.metadata.tables["auth_sessions"].c.token_hash.unique is True


def test_database_urls_are_masked_in_settings_repr():
    admin_url = "mysql+asyncmy://admin:secret@localhost/database"
    read_url = "mysql+asyncmy://reader:secret@localhost/database"
    settings = Settings(
        _env_file=None,
        database_url=admin_url,
        database_read_url=read_url,
    )

    assert admin_url not in repr(settings)
    assert read_url not in repr(settings)
    assert settings.database_url_value == admin_url
    assert settings.database_read_url_value == read_url


def test_seed_bundle_is_deterministic_and_contains_a_gmv_drop():
    anchor = date(2026, 7, 13)
    first = build_seed_bundle(anchor)
    second = build_seed_bundle(anchor)

    assert first == second
    assert len(first.products) == 6
    assert len(first.customers) == 90
    assert len(first.campaigns) == 3
    assert len(first.daily_traffic) == 540
    assert len(first.orders) == 540
    assert len(first.order_items) == 540
    assert len(first.payments) == 540
    assert all(product["stock_quantity"] >= 0 for product in first.products)
    assert first.orders[0]["id"] == 2026011401
    assert first.orders[0]["id"] == first.payments[0]["order_id"]
    assert {item["metric_code"] for item in first.metric_definitions} == {
        "gmv",
        "paid_order_count",
        "refund_amount_rate",
        "traffic_visits",
        "payment_conversion_rate",
    }

    previous_week = Decimal("0")
    latest_week = Decimal("0")
    for order in first.orders:
        if order["region"] != "华东":
            continue
        business_date = order["ordered_at"].date()
        if date(2026, 6, 29) <= business_date < date(2026, 7, 6):
            previous_week += order["order_amount"]
        elif date(2026, 7, 6) <= business_date < date(2026, 7, 13):
            latest_week += order["order_amount"]
    assert latest_week < previous_week


def test_gmv_query_uses_bound_filters_and_successful_payments():
    statement = build_gmv_query(
        datetime(2026, 7, 6),
        datetime(2026, 7, 13),
        region="华东",
    )
    compiled = statement.compile(dialect=mysql.dialect())
    sql = str(compiled)

    assert "JOIN orders" in sql
    assert "payments.payment_status" in sql
    assert "payments.paid_at >=" in sql
    assert "payments.paid_at <" in sql
    assert "orders.region" in sql
    assert "华东" not in sql
    assert "华东" in compiled.params.values()


def test_mysql_health_check_uses_and_disposes_temporary_engine(monkeypatch):
    class FakeConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def scalar(self, statement):
            assert str(statement) == "SELECT 1"
            return 1

    class FakeEngine:
        def __init__(self):
            self.disposed = False

        def connect(self):
            return FakeConnection()

        async def dispose(self):
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(
        "app.db.health.create_async_engine",
        lambda *args, **kwargs: engine,
    )

    assert asyncio.run(check_mysql_connection(Settings(_env_file=None))) is True
    assert engine.disposed is True


def test_upsert_only_updates_columns_present_in_input_rows():
    class FakeConnection:
        statement = None

        async def execute(self, statement):
            self.statement = statement

    connection = FakeConnection()
    rows = [
        {
            "id": 1,
            "metric_code": "gmv",
            "metric_name": "支付 GMV",
            "description": "支付成功金额合计",
            "formula": "SUM(paid_amount)",
            "unit": "元",
            "time_grain": "day",
            "source_tables": ["payments"],
            "version": "1.0",
            "is_active": True,
        }
    ]

    assert asyncio.run(upsert_rows(connection, MetricDefinition, rows)) == 1
    sql = str(connection.statement.compile(dialect=mysql.dialect()))
    update_clause = sql.split("ON DUPLICATE KEY UPDATE", maxsplit=1)[1]
    assert "updated_at" not in update_clause
