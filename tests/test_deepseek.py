"""DeepSeek 请求构造、知识约束和响应解析测试。"""

import asyncio
import json

import httpx

from app.config import Settings
from app.rag.deepseek import DeepSeekClient
from app.rag.models import RankedKnowledge, RetrievalHit


def make_ranked_knowledge() -> list[RankedKnowledge]:
    hit = RetrievalHit(
        chunk_id="a" * 64,
        parent_chunk_id="b" * 64,
        document_id="metric_refund_rate",
        title="退款率指标定义",
        content="退款率 = 退款成功金额 / 支付商品金额 × 100%。",
        chunk_index=0,
        doc_type="metric_definition",
        metric_name="refund_rate",
        source="指标手册",
        version="1.0",
        updated_at="2026-07-16",
        retrieval_score=0.7,
    )
    return [RankedKnowledge(hit=hit, rerank_score=0.95)]


def test_deepseek_client_builds_grounded_chat_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": "退款率按退款金额率计算。[1]"}}],
                "usage": {
                    "prompt_tokens": 30,
                    "completion_tokens": 10,
                    "total_tokens": 40,
                },
            },
        )

    async def run_test():
        settings = Settings(
            _env_file=None,
            deepseek_api_key="test-key",
            deepseek_model="deepseek-v4-pro",
            deepseek_max_retries=0,
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            return await DeepSeekClient(settings, http_client).generate(
                "退款率是什么？",
                make_ranked_knowledge(),
            )

    completion = asyncio.run(run_test())

    assert captured["authorization"] == "Bearer test-key"
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert "退款率是什么？" in captured["payload"]["messages"][1]["content"]
    assert "[资料 1]" in captured["payload"]["messages"][1]["content"]
    assert completion.answer.endswith("[1]")
    assert completion.usage["total_tokens"] == 40
