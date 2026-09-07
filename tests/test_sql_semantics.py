"""Text2SQL 业务语义契约测试。"""

import pytest

from app.analysis.models import SQLGenerationResult
from app.analysis.semantics import validate_metric_semantics
from app.db.models import MetricDefinition
from app.errors import Text2SQLGenerationError


def make_refund_metric() -> MetricDefinition:
    return MetricDefinition(
        id=3,
        metric_code="refund_amount_rate",
        metric_name="退款金额率",
        description="成功退款金额占支付成功商品金额的百分比",
        formula="100 * successful refunds / successful payments",
        unit="%",
        time_grain="day",
        source_tables=["payments", "refunds"],
        version="1.1",
        is_active=True,
    )


def make_generation(sql: str) -> SQLGenerationResult:
    return SQLGenerationResult(
        sql=sql,
        metric_code="refund_amount_rate",
        value_column="refund_amount_rate",
    )


def test_refund_rate_accepts_canonical_region_percentage_and_two_time_columns():
    generation = make_generation(
        "SELECT 100 * SUM(CASE WHEN r.refunded_at >= '2026-07-06' "
        "THEN r.refund_amount ELSE 0 END) / NULLIF(SUM(CASE WHEN "
        "p.paid_at >= '2026-07-06' THEN p.paid_amount ELSE 0 END), 0) "
        "AS refund_amount_rate FROM orders AS o JOIN payments AS p "
        "ON p.order_id = o.id LEFT JOIN refunds AS r ON r.order_id = o.id "
        "WHERE o.region = '华东'"
    )

    validate_metric_semantics(generation, make_refund_metric())


@pytest.mark.parametrize(
    "sql",
    [
        (
            "SELECT 100 * SUM(r.refund_amount) / NULLIF(SUM(p.paid_amount), 0) "
            "AS refund_amount_rate FROM orders AS o JOIN payments AS p "
            "ON p.order_id = o.id JOIN refunds AS r ON r.order_id = o.id "
            "WHERE o.region = '华东区' AND p.paid_at >= '2026-07-06' "
            "AND r.refunded_at >= '2026-07-06'"
        ),
        (
            "SELECT SUM(r.refund_amount) / NULLIF(SUM(p.paid_amount), 0) "
            "AS refund_amount_rate FROM payments AS p JOIN refunds AS r "
            "ON r.order_id = p.order_id WHERE p.paid_at >= '2026-07-06' "
            "AND r.refunded_at >= '2026-07-06'"
        ),
        (
            "SELECT 100 * SUM(r.refund_amount) / NULLIF(SUM(p.paid_amount), 0) "
            "AS refund_amount_rate FROM payments AS p JOIN refunds AS r "
            "ON r.order_id = p.order_id WHERE p.paid_at >= '2026-07-06'"
        ),
    ],
)
def test_refund_rate_rejects_noncanonical_or_incomplete_semantics(sql):
    with pytest.raises(Text2SQLGenerationError):
        validate_metric_semantics(make_generation(sql), make_refund_metric())


def test_single_explicit_date_rejects_a_generated_week_range():
    generation = SQLGenerationResult(
        sql=(
            "SELECT COUNT(DISTINCT p.order_id) AS paid_order_count "
            "FROM payments AS p WHERE p.paid_at >= '2026-07-06 00:00:00' "
            "AND p.paid_at < '2026-07-13 00:00:00'"
        ),
        metric_code="paid_order_count",
        value_column="paid_order_count",
    )
    metric = MetricDefinition(
        id=2,
        metric_code="paid_order_count",
        metric_name="支付订单量",
        description="支付成功的去重订单数",
        formula="COUNT(DISTINCT payments.order_id)",
        unit="单",
        time_grain="day",
        source_tables=["payments"],
        version="1.0",
        is_active=True,
    )

    with pytest.raises(Text2SQLGenerationError):
        validate_metric_semantics(generation, metric, "2026-07-12 全站支付订单量")

    generation.sql = (
        "SELECT COUNT(DISTINCT p.order_id) AS paid_order_count "
        "FROM payments AS p WHERE p.paid_at >= '2026-07-12 00:00:00' "
        "AND p.paid_at < '2026-07-13 00:00:00'"
    )
    validate_metric_semantics(generation, metric, "2026-07-12 全站支付订单量")
