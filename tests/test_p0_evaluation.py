"""P0 验收数据集和纯离线评测函数测试。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from scripts.evaluate_p0 import (
    evaluate_intents,
    evaluate_sql_safety,
    evaluate_tool_selection,
)


CASES_PATH = Path("data/evaluation/p0_acceptance_cases.json")


def load_cases():
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


class FakeCatalogClassifier:
    def __init__(self, cases):
        self.expected = {case["question"]: case["expected_intent"] for case in cases}

    async def classify(self, question, history_messages=None):
        intent = self.expected[question]
        return SimpleNamespace(
            result=SimpleNamespace(
                intent=intent,
                confidence=1.0,
                rewritten_question=question,
                reason="评测函数测试桩",
            ),
            model="deepseek-intent-test",
            usage={"total_tokens": 1},
        )


def test_p0_intent_catalog_has_fifty_cases_and_meets_target():
    cases = load_cases()["intent_cases"]
    report = asyncio.run(evaluate_intents(cases, FakeCatalogClassifier(cases)))

    assert report["case_count"] == 50
    assert report["accuracy"] >= 0.90


def test_p0_dangerous_sql_catalog_is_fully_blocked():
    report = evaluate_sql_safety(load_cases()["dangerous_sql_cases"])

    assert report["case_count"] == 20
    assert report["block_rate"] == 1.0


def test_p0_tool_selection_meets_target():
    cases = load_cases()["intent_cases"]
    intent_report = asyncio.run(evaluate_intents(cases, FakeCatalogClassifier(cases)))
    report = evaluate_tool_selection(intent_report)

    assert report["case_count"] == 50
    assert report["accuracy"] >= 0.90
    assert {row["actual_tool"] for row in report["cases"]} == {
        "knowledge_retrieval",
        "execute_readonly_sql",
        "forecast_sales",
        "none",
    }
