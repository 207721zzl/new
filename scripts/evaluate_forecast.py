"""运行动态区间销量预测黄金用例并生成验收报告。"""

import asyncio
import json
from datetime import UTC, datetime

from app.config import PROJECT_ROOT
from app.db.session import dispose_database_engines
from app.forecast.service import SalesForecastService


async def evaluate() -> dict:
    """逐条执行真实只读数据与本地模型预测链路。"""
    cases = json.loads(
        (PROJECT_ROOT / "data" / "evaluation" / "forecast_cases.json").read_text(
            encoding="utf-8"
        )
    )
    service = SalesForecastService()
    results = []
    for case in cases:
        try:
            answer = await service.answer(case["question"])
            forecast = answer.forecast
            passed = (
                forecast.horizon_days == case["horizon_days"]
                and forecast.subject_name == case["subject_name"]
                and len(forecast.points) == case["horizon_days"]
                and forecast.generation_method == "torch_global"
                and all(
                    point.lower_bound
                    <= point.predicted_quantity
                    <= point.upper_bound
                    for point in forecast.points
                )
            )
            results.append(
                {
                    **case,
                    "passed": passed,
                    "actual_horizon_days": forecast.horizon_days,
                    "actual_subject_name": forecast.subject_name,
                    "generation_method": forecast.generation_method,
                    "history_days": forecast.history_days,
                    "model_version": forecast.model_version,
                    "recommended_quantity": forecast.replenishment.recommended_quantity,
                    "error": None,
                }
            )
        except Exception as exc:
            results.append({**case, "passed": False, "error": type(exc).__name__})
    passed_count = sum(bool(row["passed"]) for row in results)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": passed_count == len(results),
        "case_count": len(results),
        "passed_count": passed_count,
        "cases": results,
    }
    report_path = PROJECT_ROOT / "reports" / "forecast_acceptance_evaluation.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {**report, "report": str(report_path)}


async def run() -> dict:
    """在同一事件循环完成评测并释放数据库连接池。"""
    try:
        return await evaluate()
    finally:
        await dispose_database_engines()


def main() -> int:
    result = asyncio.run(run())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
