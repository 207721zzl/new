"""最终发布验收汇总和 P95 口径测试。"""

from scripts.evaluate_release import build_release_report, percentile_nearest_rank


def test_nearest_rank_percentile_is_deterministic():
    assert percentile_nearest_rank([1, 2, 3, 4, 100], 0.95) == 100
    assert percentile_nearest_rank([], 0.95) is None


def test_release_report_fails_only_the_metric_below_target():
    p0 = {
        "summary": {
            "intent_accuracy": 1.0,
            "dangerous_sql_block_rate": 1.0,
            "fact_consistency_rate": 1.0,
            "rag_recall_at_5": 1.0,
            "tool_selection_accuracy": 1.0,
        }
    }
    forecast = {
        "passed_count": 10,
        "case_count": 10,
        "cases": [{"generation_method": "torch_global"}] * 10,
    }
    sql = {
        "accuracy": 0.9,
        "cases": [{"latency_ms": value} for value in (1000, 2000, 20_000)],
    }

    report = build_release_report(p0, forecast, sql)

    assert report["evaluation_context"]["is_blind"] is False
    assert (
        report["evaluation_context"]["generalization_claim_allowed"] is False
    )
    assert report["passed"] is False
    assert report["checks"]["ordinary_query_p95_ms"] is False
    assert all(
        value
        for key, value in report["checks"].items()
        if key != "ordinary_query_p95_ms"
    )


def test_release_report_rejects_legacy_deepseek_forecast_report():
    p0 = {
        "summary": {
            "intent_accuracy": 1.0,
            "dangerous_sql_block_rate": 1.0,
            "fact_consistency_rate": 1.0,
            "rag_recall_at_5": 1.0,
            "tool_selection_accuracy": 1.0,
        }
    }
    forecast = {
        "passed_count": 10,
        "case_count": 10,
        "cases": [{"generation_method": "deepseek"}] * 10,
    }
    sql = {"accuracy": 1.0, "cases": [{"latency_ms": 1000}]}

    report = build_release_report(p0, forecast, sql)

    assert report["passed"] is False
    assert report["metrics"]["forecast_contract_rate"] == 0
    assert report["checks"]["forecast_contract_rate"] is False
