"""SQL 执行正确率黄金集和数值比较测试。"""

import json
from pathlib import Path

from app.analysis.safety import SQLSafetyValidator
from scripts.evaluate_sql_execution import values_match


def test_sql_execution_catalog_has_safe_truth_queries():
    cases = json.loads(
        Path("data/evaluation/sql_execution_cases.json").read_text(encoding="utf-8")
    )

    assert len(cases) == 12
    assert {case["metric_code"] for case in cases} == {
        "gmv",
        "paid_order_count",
        "refund_amount_rate",
        "traffic_visits",
        "payment_conversion_rate",
    }
    for case in cases:
        validated = SQLSafetyValidator().validate(case["truth_sql"])
        assert validated.sql.endswith("LIMIT 1000")


def test_values_match_uses_explicit_absolute_tolerance():
    assert values_match(100.001, 100, 0.01) is True
    assert values_match(100.02, 100, 0.01) is False
    assert values_match(True, 1, 0) is False
