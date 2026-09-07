"""DeepSeek 结构化 Text2SQL Prompt 与响应校验测试。"""

import asyncio
import json
from datetime import date

import httpx
import pytest

from app.analysis.deepseek import Text2SQLClient
from app.analysis.models import Text2SQLRepairContext
from app.config import Settings
from app.db.models import MetricDefinition
from app.errors import Text2SQLGenerationError


def make_gmv_metric() -> MetricDefinition:
    return MetricDefinition(
        id=1,
        metric_code="gmv",
        metric_name="支付 GMV",
        description="支付成功订单的实付金额合计",
        formula="SUM(payments.paid_amount) WHERE payment_status = 'success'",
        unit="元",
        time_grain="day",
        source_tables=["orders", "payments"],
        version="1.0",
        is_active=True,
    )


def test_deepseek_returns_strict_structured_sql_result():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "sql": (
                                        "SELECT COALESCE(SUM(p.paid_amount), 0) AS gmv "
                                        "FROM payments p JOIN orders o "
                                        "ON o.id = p.order_id"
                                    ),
                                    "metric_code": "gmv",
                                    "value_column": "gmv",
                                    "assumptions": ["上周按自然周计算"],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 88},
            },
        )

    async def run_test():
        settings = Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            deepseek_max_retries=0,
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            client = Text2SQLClient(
                settings,
                http_client,
                today_provider=lambda: date(2026, 7, 18),
            )
            return await client.generate("上周华东区 GMV 是多少？", [make_gmv_metric()])

    completion = asyncio.run(run_test())
    payload = captured["payload"]
    prompt = payload["messages"][1]["content"]

    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0
    assert "payments.paid_amount" in prompt
    assert "SUM(payments.paid_amount)" in prompt
    assert "2026-07-06 00:00:00" in prompt
    assert "2026-07-13 00:00:00" in prompt
    assert "用户问题：上周华东 GMV 是多少？" in prompt
    assert "区域维度只允许使用标准值“华东”“华南”“华北”" in payload["messages"][0]["content"]
    assert "单位为“%”的指标必须返回百分数" in payload["messages"][0]["content"]
    assert completion.result.metric_code == "gmv"
    assert completion.result.value_column == "gmv"
    assert completion.usage == {"total_tokens": 88}


def test_invalid_deepseek_json_is_rejected():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not-json"}}]},
        )

    async def run_test():
        settings = Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            deepseek_max_retries=0,
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await Text2SQLClient(settings, http_client).generate(
                "上周 GMV？", [make_gmv_metric()]
            )

    with pytest.raises(Text2SQLGenerationError):
        asyncio.run(run_test())


def test_comparison_prompt_and_response_use_two_period_columns():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "sql": (
                                        "SELECT 22682 AS current_value, "
                                        "18000 AS previous_value FROM payments"
                                    ),
                                    "metric_code": "gmv",
                                    "value_column": "current_value",
                                    "analysis_type": "comparison",
                                    "current_value_column": "current_value",
                                    "previous_value_column": "previous_value",
                                    "current_period": "2026-07-06 至 2026-07-13",
                                    "previous_period": "2026-06-29 至 2026-07-06",
                                    "assumptions": ["按完整自然周比较"],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    async def run_test():
        settings = Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            deepseek_max_retries=0,
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            client = Text2SQLClient(
                settings,
                http_client,
                today_provider=lambda: date(2026, 7, 18),
            )
            return await client.generate(
                "上周华东区 GMV 与前一周相比变化如何？",
                [make_gmv_metric()],
                analysis_type="comparison",
            )

    completion = asyncio.run(run_test())
    prompt = captured["payload"]["messages"][1]["content"]

    assert "要求的分析类型：comparison" in prompt
    assert "2026-06-29 00:00:00" in prompt
    assert "2026-06-01 00:00:00" in prompt
    assert "2026-05-01 00:00:00" in prompt
    assert "2025-06-01 00:00:00" in prompt
    assert "current_value" in prompt
    assert "previous_value" in prompt
    assert "不要在 SQL 中计算差值或增长率" in prompt
    assert completion.result.analysis_type == "comparison"
    assert completion.result.previous_value_column == "previous_value"


def test_repair_context_is_added_to_prompt_without_internal_error_details():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "sql": "SELECT SUM(p.paid_amount) AS gmv FROM payments p",
                                    "metric_code": "gmv",
                                    "value_column": "gmv",
                                }
                            )
                        }
                    }
                ]
            },
        )

    async def run_test():
        settings = Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            deepseek_max_retries=0,
        )
        context = Text2SQLRepairContext(
            attempt=1,
            failure_type="unsafe_sql",
            previous_sql="DELETE FROM orders",
            instruction="仅生成白名单 SELECT。",
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await Text2SQLClient(settings, http_client).generate(
                "上周 GMV？",
                [make_gmv_metric()],
                repair_context=context,
            )

    asyncio.run(run_test())
    prompt = captured["payload"]["messages"][1]["content"]

    assert "这是自动修复请求" in prompt
    assert "失败类型：unsafe_sql" in prompt
    assert "上一次 SQL：DELETE FROM orders" in prompt
    assert "仅生成白名单 SELECT" in prompt
