"""查询重写客户端和 RAG 失败回退测试。"""

import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.errors import QueryRewriteError
from app.rag.query_rewriter import DeepSeekQueryRewriter


def test_query_rewriter_calls_json_output_and_returns_metadata():
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
                                    "rewritten_query": (
                                        "退款率（退款金额率）的指标定义、计算公式与统计口径"
                                    ),
                                    "reason": "补充同义表达并明确检索目标",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 20, "total_tokens": 32},
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
            rewriter = DeepSeekQueryRewriter(settings, http_client)
            return await rewriter.rewrite("退款率咋算？")

    outcome = asyncio.run(run_test())

    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["temperature"] == 0
    assert "退款率咋算" in captured["payload"]["messages"][1]["content"]
    assert outcome.original_query == "退款率咋算？"
    assert outcome.rewritten_query.startswith("退款率（退款金额率）")
    assert outcome.applied is True
    assert outcome.fallback is False
    assert outcome.usage == {"prompt_tokens": 20, "total_tokens": 32}


def test_query_rewriter_rejects_invalid_contract():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"answer": "42"}'}}]},
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
            await DeepSeekQueryRewriter(settings, http_client).rewrite("退款率")

    with pytest.raises(QueryRewriteError):
        asyncio.run(run_test())
