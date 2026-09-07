"""汇总 P0、预测、SQL 与性能结果，生成最终发布验收报告。"""

import json
import math
from datetime import UTC, datetime
from typing import Any

from app.config import PROJECT_ROOT


TARGETS = {
    "intent_accuracy": 0.90,
    "dangerous_sql_block_rate": 1.0,
    "fact_consistency_rate": 0.95,
    "rag_recall_at_5": 0.85,
    "tool_selection_accuracy": 0.90,
    "forecast_contract_rate": 1.0,
    "sql_execution_accuracy": 0.80,
    "ordinary_query_p95_ms": 15_000,
}


def percentile_nearest_rank(values: list[float], percentile: float) -> float | None:
    """使用可复现的 nearest-rank 口径计算百分位。"""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return round(ordered[rank - 1], 3)


def load_report(name: str) -> dict[str, Any]:
    path = PROJECT_ROOT / "reports" / name
    return json.loads(path.read_text(encoding="utf-8"))


def build_release_report(
    p0: dict[str, Any],
    forecast: dict[str, Any],
    sql: dict[str, Any],
) -> dict[str, Any]:
    p0_summary = p0["summary"]
    forecast_cases = forecast.get("cases", [])
    forecast_report_compatible = (
        bool(forecast_cases)
        and len(forecast_cases) == forecast.get("case_count")
        and all(
            case.get("generation_method") == "torch_global"
            for case in forecast_cases
        )
    )
    forecast_rate = (
        forecast["passed_count"] / forecast["case_count"]
        if forecast["case_count"] and forecast_report_compatible
        else 0.0
    )
    latencies = [
        float(case["latency_ms"])
        for case in sql["cases"]
        if case.get("latency_ms") is not None
    ]
    ordinary_p95 = percentile_nearest_rank(latencies, 0.95)
    metrics = {
        "intent_accuracy": p0_summary["intent_accuracy"],
        "dangerous_sql_block_rate": p0_summary["dangerous_sql_block_rate"],
        "fact_consistency_rate": p0_summary["fact_consistency_rate"],
        "rag_recall_at_5": p0_summary["rag_recall_at_5"],
        "tool_selection_accuracy": p0_summary["tool_selection_accuracy"],
        "forecast_contract_rate": round(forecast_rate, 4),
        "sql_execution_accuracy": sql["accuracy"],
        "ordinary_query_p95_ms": ordinary_p95,
    }
    checks = {
        "intent_accuracy": metrics["intent_accuracy"] >= TARGETS["intent_accuracy"],
        "dangerous_sql_block_rate": metrics["dangerous_sql_block_rate"]
        >= TARGETS["dangerous_sql_block_rate"],
        "fact_consistency_rate": metrics["fact_consistency_rate"]
        >= TARGETS["fact_consistency_rate"],
        "rag_recall_at_5": metrics["rag_recall_at_5"]
        >= TARGETS["rag_recall_at_5"],
        "tool_selection_accuracy": metrics["tool_selection_accuracy"]
        >= TARGETS["tool_selection_accuracy"],
        "forecast_contract_rate": metrics["forecast_contract_rate"]
        >= TARGETS["forecast_contract_rate"],
        "sql_execution_accuracy": metrics["sql_execution_accuracy"]
        >= TARGETS["sql_execution_accuracy"],
        "ordinary_query_p95_ms": ordinary_p95 is not None
        and ordinary_p95 <= TARGETS["ordinary_query_p95_ms"],
    }
    return {
        "evaluation_context": {
            "evaluation_kind": "development_regression_aggregate",
            "is_blind": False,
            "labels_visible_during_development": True,
            "generalization_claim_allowed": False,
            "interpretation": (
                "汇总来源均为开发验收数据，只能作为发布回归门禁，"
                "不能作为真实准确率或泛化能力证明。"
            ),
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": all(checks.values()),
        "targets": TARGETS,
        "checks": checks,
        "metrics": metrics,
        "source_reports": {
            "p0": "p0_acceptance_evaluation.json",
            "forecast": "forecast_acceptance_evaluation.json",
            "sql_execution": "sql_execution_evaluation.json",
        },
        "notes": [
            "ordinary_query_p95_ms 使用 SQL 执行正确率用例的完整 Text2SQL 服务耗时。",
            "预测报告必须由 torch_global 预测层生成；旧 DeepSeek 数值预测报告不参与发布验收。",
            "P10/P90 分位数区间需通过留出回测验证覆盖率后再解释为已校准区间。",
        ],
    }


def main() -> int:
    report = build_release_report(
        load_report("p0_acceptance_evaluation.json"),
        load_report("forecast_acceptance_evaluation.json"),
        load_report("sql_execution_evaluation.json"),
    )
    path = PROJECT_ROOT / "reports" / "release_acceptance_evaluation.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**report, "report": str(path)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
