"""真实执行 Text2SQL 与独立黄金 SQL，并比较数值正确率。"""

import argparse
import asyncio
import json
from numbers import Real
from pathlib import Path
from time import perf_counter

from app.analysis.executor import ReadOnlySQLExecutor
from app.analysis.safety import SQLSafetyValidator
from app.analysis.service import DataAnalysisService
from app.config import PROJECT_ROOT, get_settings
from app.db.session import dispose_database_engines, get_read_session_factory


TARGET_ACCURACY = 0.80


def values_match(actual: int | float, expected: int | float, tolerance: float) -> bool:
    """按绝对误差比较确定性数值，拒绝布尔值等伪数值。"""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    if not isinstance(actual, Real) or not isinstance(expected, Real):
        return False
    return abs(float(actual) - float(expected)) <= tolerance


async def execute_truth(sql: str) -> int | float:
    settings = get_settings()
    validated = SQLSafetyValidator(settings.sql_query_limit).validate(sql)
    executor = ReadOnlySQLExecutor(settings)
    async with get_read_session_factory()() as session:
        result = await executor.execute(session, validated)
    if not result.rows or "expected_value" not in result.rows[0]:
        raise ValueError("truth SQL must return expected_value")
    return result.rows[0]["expected_value"]


async def evaluate(cases_path: Path, case_ids: set[str] | None = None) -> dict:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    if case_ids:
        cases = [case for case in cases if case["id"] in case_ids]
    service = DataAnalysisService()
    rows = []
    for case in cases:
        started_at = perf_counter()
        try:
            expected = await execute_truth(case["truth_sql"])
            answer = await service.answer(case["question"])
            actual = answer.analysis.value
            actual_metric = answer.analysis.evidence.metric.code
            passed = (
                actual_metric == case["metric_code"]
                and values_match(actual, expected, float(case["tolerance"]))
            )
            rows.append(
                {
                    "id": case["id"],
                    "question": case["question"],
                    "metric_code": case["metric_code"],
                    "actual_metric_code": actual_metric,
                    "expected_value": expected,
                    "actual_value": actual,
                    "generated_sql": answer.analysis.sql,
                    "repair_count": answer.analysis.repair_count,
                    "latency_ms": round((perf_counter() - started_at) * 1000, 3),
                    "passed": passed,
                    "error": None,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "id": case["id"],
                    "question": case["question"],
                    "metric_code": case["metric_code"],
                    "latency_ms": round((perf_counter() - started_at) * 1000, 3),
                    "passed": False,
                    "error": type(exc).__name__,
                }
            )
    passed_count = sum(bool(row["passed"]) for row in rows)
    accuracy = round(passed_count / len(rows), 4) if rows else 0.0
    return {
        "passed": accuracy >= TARGET_ACCURACY,
        "target_accuracy": TARGET_ACCURACY,
        "case_count": len(rows),
        "passed_count": passed_count,
        "accuracy": accuracy,
        "cases": rows,
    }


async def run(cases_path: Path, case_ids: set[str] | None = None) -> dict:
    try:
        return await evaluate(cases_path, case_ids)
    finally:
        await dispose_database_engines()


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate Text2SQL execution accuracy.")
    parser.add_argument(
        "--cases",
        type=Path,
        default=PROJECT_ROOT / "data" / "evaluation" / "sql_execution_cases.json",
    )
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "reports" / "sql_execution_evaluation.json",
    )
    args = parser.parse_args()
    cases_path = args.cases if args.cases.is_absolute() else PROJECT_ROOT / args.cases
    report = asyncio.run(run(cases_path, set(args.case_id) or None))
    report_path = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({**report, "report": str(report_path)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
