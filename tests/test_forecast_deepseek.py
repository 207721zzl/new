"""DeepSeek 预测解释 Prompt、严格 JSON 和 Token 元数据测试。"""

import asyncio
from datetime import date
import json

import httpx
import pytest

from app.config import Settings
from app.errors import ForecastGenerationError
from app.forecast.deepseek import ForecastDeepSeekExplainer
from app.forecast.models import ForecastPoint


POINTS = [
    ForecastPoint(
        date=date(2026, 7, 13),
        predicted_quantity=12,
        lower_bound=9,
        upper_bound=15,
    ),
    ForecastPoint(
        date=date(2026, 7, 14),
        predicted_quantity=13,
        lower_bound=10,
        upper_bound=16,
    ),
]


def test_forecast_explainer_treats_torch_numbers_as_readonly_evidence():
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
                                    "reasoning_summary": "本地模型显示近期需求平稳。",
                                    "assumptions": ["活动力度不变"],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 456},
            },
        )

    async def run_test():
        settings = Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            deepseek_max_retries=0,
            forecast_explanation_max_tokens=800,
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            return await ForecastDeepSeekExplainer(settings, http_client).explain(
                question="预测未来两天耳机销量",
                subject_name="耳机",
                subject_type="category",
                forecast_start_date=date(2026, 7, 13),
                forecast_end_date=date(2026, 7, 14),
                current_stock=80,
                history=[{"date": "2026-07-12", "quantity": 11}],
                forecast_points=POINTS,
                model_version="torch-global-test",
                recommended_quantity=20,
            )

    completion = asyncio.run(run_test())
    payload = captured["payload"]
    system_prompt = payload["messages"][0]["content"]
    user_prompt = payload["messages"][1]["content"]

    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0.1
    assert payload["max_tokens"] == 800
    assert "不得修改" in system_prompt
    assert "torch-global-test" in user_prompt
    assert '"predicted_quantity": 12.0' in user_prompt
    assert completion.result.reasoning_summary == "本地模型显示近期需求平稳。"
    assert completion.usage == {"total_tokens": 456}


def test_forecast_explainer_rejects_numeric_prediction_fields():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "daily_forecast": [
                                        {
                                            "date": "2026-07-13",
                                            "predicted_quantity": 99,
                                        }
                                    ],
                                    "reasoning_summary": "invalid",
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
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            await ForecastDeepSeekExplainer(settings, http_client).explain(
                question="预测销量",
                subject_name="耳机",
                subject_type="category",
                forecast_start_date=date(2026, 7, 13),
                forecast_end_date=date(2026, 7, 13),
                current_stock=80,
                history=[{"date": "2026-07-12", "quantity": 11}],
                forecast_points=POINTS[:1],
                model_version="torch-global-test",
                recommended_quantity=0,
            )

    with pytest.raises(ForecastGenerationError):
        asyncio.run(run_test())
