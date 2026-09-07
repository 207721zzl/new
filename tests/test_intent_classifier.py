"""DeepSeek 结构化意图识别与多轮问题改写测试。"""

import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.errors import IntentClassificationError
from app.intent.deepseek import DeepSeekIntentClassifier


def test_intent_classifier_calls_deepseek_json_output_with_history():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
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
                                    "intent": "data_analysis",
                                    "confidence": 0.97,
                                    "rewritten_question": "查询上周华南地区的支付 GMV",
                                    "reason": "结合上一轮后是在查询另一区域的经营数值",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                },
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
            return await DeepSeekIntentClassifier(settings, http_client).classify(
                "那华南呢？",
                [
                    {"role": "user", "content": "查询上周华东地区的支付 GMV"},
                    {"role": "assistant", "content": "支付 GMV 为 100 元"},
                ],
            )

    completion = asyncio.run(run_test())

    assert captured["authorization"] == "Bearer test-key"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["temperature"] == 0
    user_prompt = captured["payload"]["messages"][1]["content"]
    assert "那华南呢" in user_prompt
    assert "上周华东地区" in user_prompt
    assert completion.result.intent == "data_analysis"
    assert completion.result.rewritten_question == "查询上周华南地区的支付 GMV"
    assert completion.usage == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
    }


def test_intent_classifier_rejects_unknown_intent():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "write_database",
                                    "confidence": 1,
                                    "rewritten_question": "删除订单",
                                    "reason": "用户要求删除数据",
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
            await DeepSeekIntentClassifier(settings, http_client).classify("删除订单")

    with pytest.raises(IntentClassificationError):
        asyncio.run(run_test())
