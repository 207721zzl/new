"""运行 P0 意图、安全、事实一致性和现有 RAG 指标验收。"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.analysis.root_cause import RootCauseAnalysisService
from app.analysis.safety import SQLSafetyValidator
from app.config import PROJECT_ROOT
from app.errors import UnsafeSQLError
from app.intent.deepseek import DeepSeekIntentClassifier, get_intent_classifier


TARGETS = {
    "intent_accuracy": 0.90,
    "dangerous_sql_block_rate": 1.0,
    "fact_consistency_rate": 0.95,
    "rag_recall_at_5": 0.85,
    "tool_selection_accuracy": 0.90,
    "golden_question_min": 80,
    "golden_question_max": 100,
}

INTENT_TOOL_MAP = {
    "knowledge_qa": "knowledge_retrieval",
    "data_analysis": "execute_readonly_sql",
    "comparison_analysis": "execute_readonly_sql",
    "root_cause_analysis": "execute_readonly_sql",
    "sales_forecast": "forecast_sales",
    "out_of_scope": "none",
}


def resolve_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_json(path: Path) -> Any:
    return json.loads(resolve_path(path).read_text(encoding="utf-8"))


async def evaluate_intents(
    cases: list[dict[str, Any]],
    classifier: DeepSeekIntentClassifier | Any | None = None,
) -> dict[str, Any]:
    """调用与生产工作流相同的 DeepSeek 分类器评测意图。"""
    classifier = classifier or get_intent_classifier()
    rows = []
    for case in cases:
        try:
            completion = await classifier.classify(case["question"], [])
            decision = completion.result
            rows.append(
                {
                    **case,
                    "actual_intent": decision.intent,
                    "confidence": decision.confidence,
                    "rewritten_question": decision.rewritten_question,
                    "reason": decision.reason,
                    "model": completion.model,
                    "usage": completion.usage,
                    "passed": decision.intent == case["expected_intent"],
                    "error": None,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    **case,
                    "actual_intent": None,
                    "confidence": None,
                    "rewritten_question": None,
                    "reason": None,
                    "model": None,
                    "usage": None,
                    "passed": False,
                    "error": type(exc).__name__,
                }
            )
    passed = sum(row["passed"] for row in rows)
    return {
        "case_count": len(rows),
        "passed_count": passed,
        "accuracy": round(passed / len(rows), 4) if rows else 0.0,
        "cases": rows,
    }


def evaluate_sql_safety(cases: list[dict[str, Any]]) -> dict[str, Any]:
    validator = SQLSafetyValidator()
    rows = []
    for case in cases:
        blocked = False
        try:
            validator.validate(case["sql"])
        except UnsafeSQLError:
            blocked = True
        rows.append({**case, "blocked": blocked, "passed": blocked})
    passed = sum(row["passed"] for row in rows)
    return {
        "case_count": len(rows),
        "blocked_count": passed,
        "block_rate": round(passed / len(rows), 4) if rows else 0.0,
        "cases": rows,
    }


def evaluate_tool_selection(intent_report: dict[str, Any]) -> dict[str, Any]:
    """根据同一次大模型意图评测结果验证工具分支，避免重复调用模型。"""
    rows = []
    for case in intent_report["cases"]:
        actual_intent = case["actual_intent"]
        expected_tool = INTENT_TOOL_MAP[case["expected_intent"]]
        actual_tool = INTENT_TOOL_MAP.get(actual_intent, "classification_failed")
        rows.append(
            {
                "id": case["id"],
                "question": case["question"],
                "expected_tool": expected_tool,
                "actual_tool": actual_tool,
                "passed": actual_tool == expected_tool,
            }
        )
    passed = sum(row["passed"] for row in rows)
    return {
        "case_count": len(rows),
        "passed_count": passed,
        "accuracy": round(passed / len(rows), 4) if rows else 0.0,
        "cases": rows,
    }


def _fact_numbers_match(result) -> bool:
    analysis = result.analysis
    root_cause = analysis.root_cause
    if root_cause is None or analysis.comparison is None:
        return False
    baseline = analysis.evidence.rows[0] if analysis.evidence.rows else {}
    if baseline.get("current_value") != analysis.comparison.current_value:
        return False
    if baseline.get("previous_value") != analysis.comparison.previous_value:
        return False
    for breakdown in root_cause.breakdowns:
        if not breakdown.reconciled:
            return False
        raw_rows = {
            str(row.get("dimension_value") or "未分类"): row
            for row in breakdown.evidence.rows
        }
        for item in breakdown.items:
            raw = raw_rows.get(item.name)
            if raw is None:
                return False
            if raw.get("current_value") != item.current_value:
                return False
            if raw.get("previous_value") != item.previous_value:
                return False
    return True


async def evaluate_fact_consistency(cases: list[dict[str, Any]]) -> dict[str, Any]:
    service = RootCauseAnalysisService()
    rows = []
    for case in cases:
        try:
            result = await service.answer(case["question"])
            metric_code = result.analysis.evidence.metric.code
            passed = metric_code == case["metric_code"] and _fact_numbers_match(result)
            rows.append(
                {
                    **case,
                    "actual_metric_code": metric_code,
                    "passed": passed,
                    "error": None,
                }
            )
        except Exception as exc:  # 报告只记录稳定异常类型，不写连接和堆栈。
            rows.append(
                {
                    **case,
                    "actual_metric_code": None,
                    "passed": False,
                    "error": type(exc).__name__,
                }
            )
    passed = sum(row["passed"] for row in rows)
    return {
        "case_count": len(rows),
        "passed_count": passed,
        "consistency_rate": round(passed / len(rows), 4) if rows else 0.0,
        "cases": rows,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate InsightAgent P0 acceptance.")
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("data/evaluation/p0_acceptance_cases.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/p0_acceptance_evaluation.json"),
    )
    parser.add_argument(
        "--skip-live-facts",
        action="store_true",
        help="Skip MySQL-backed root-cause fact checks.",
    )
    return parser.parse_args()


async def run_evaluation(args: argparse.Namespace) -> dict[str, Any]:
    """执行包含在线 DeepSeek 意图识别的完整 P0 评测。"""
    cases = load_json(args.cases)
    classifier = get_intent_classifier()
    intent = await evaluate_intents(cases["intent_cases"], classifier)
    tool_selection = evaluate_tool_selection(intent)
    sql_safety = evaluate_sql_safety(cases["dangerous_sql_cases"])
    if args.skip_live_facts:
        facts = {
            "case_count": len(cases["fact_consistency_cases"]),
            "passed_count": 0,
            "consistency_rate": None,
            "cases": [],
            "skipped": True,
        }
    else:
        facts = await evaluate_fact_consistency(cases["fact_consistency_cases"])

    comparison_cases = load_json(Path("data/evaluation/comparison_cases.json"))
    comparison_intent = await evaluate_intents(comparison_cases, classifier)
    rag_questions = load_json(Path("data/knowledge/test_questions.json"))
    retrieval_report = load_json(Path("reports/retrieval_evaluation.json"))
    rag_recall_at_5 = retrieval_report["summary"]["reranked"]["recall_at_5"]
    golden_question_count = (
        len(cases["intent_cases"]) + len(comparison_cases) + len(rag_questions)
    )
    catalog_case_count = (
        golden_question_count
        + len(cases["dangerous_sql_cases"])
        + len(cases["fact_consistency_cases"])
    )

    checks = {
        "intent_accuracy": intent["accuracy"] >= TARGETS["intent_accuracy"],
        "comparison_route_accuracy": comparison_intent["accuracy"] == 1.0,
        "dangerous_sql_block_rate": (
            sql_safety["block_rate"] >= TARGETS["dangerous_sql_block_rate"]
        ),
        "fact_consistency_rate": (
            facts["consistency_rate"] is not None
            and facts["consistency_rate"] >= TARGETS["fact_consistency_rate"]
        ),
        "rag_recall_at_5": rag_recall_at_5 >= TARGETS["rag_recall_at_5"],
        "tool_selection_accuracy": (
            tool_selection["accuracy"] >= TARGETS["tool_selection_accuracy"]
        ),
        "golden_question_count": (
            TARGETS["golden_question_min"]
            <= golden_question_count
            <= TARGETS["golden_question_max"]
        ),
    }
    report = {
        "evaluation_context": {
            "evaluation_kind": "development_regression",
            "is_blind": False,
            "labels_visible_during_development": True,
            "generalization_claim_allowed": False,
            "interpretation": (
                "仅用于固定验收链路回归；不得作为真实准确率或泛化能力证明。"
            ),
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "targets": TARGETS,
        "summary": {
            "passed": all(checks.values()),
            "checks": checks,
            "golden_question_count": golden_question_count,
            "catalog_case_count": catalog_case_count,
            "intent_accuracy": intent["accuracy"],
            "comparison_route_accuracy": comparison_intent["accuracy"],
            "dangerous_sql_block_rate": sql_safety["block_rate"],
            "fact_consistency_rate": facts["consistency_rate"],
            "rag_recall_at_5": rag_recall_at_5,
            "tool_selection_accuracy": tool_selection["accuracy"],
            "rag_report_generated_at": retrieval_report.get("generated_at"),
        },
        "intent": intent,
        "comparison_intent": comparison_intent,
        "sql_safety": sql_safety,
        "fact_consistency": facts,
        "tool_selection": tool_selection,
    }
    return report


def main() -> int:
    args = parse_args()
    report = asyncio.run(run_evaluation(args))
    output_path = resolve_path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({**report["summary"], "report": str(output_path)}))
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
