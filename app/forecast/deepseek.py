"""DeepSeek 销量预测解释客户端；不参与任何数值预测。"""

from datetime import date
import json
from typing import Any

import httpx
from pydantic import ValidationError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_exponential

from app.config import Settings, get_settings
from app.errors import (
    ConfigurationError,
    ForecastGenerationError,
)
from app.forecast.models import (
    ForecastExplanation,
    ForecastExplanationCompletion,
    ForecastPoint,
)
from app.logging_config import get_logger
from app.rag.deepseek import RetryableDeepSeekError


logger = get_logger("forecast.deepseek")

FORECAST_EXPLANATION_SYSTEM_PROMPT = """你是经营分析系统中的销量预测解释助手。
所有预测数字均已由本地全局 PyTorch 模型生成，你不得修改、补充、重算或替换任何预测值。
你只能依据提供的历史摘要、PyTorch 预测、库存和补货结果解释趋势、区间和业务假设。
不得声称自己生成了销量预测；不得把 P10/P90 分位数区间描述为统计置信区间。
reasoning_summary 简洁说明可由证据支持的趋势与波动；assumptions 只列业务假设和模型边界。
只返回包含 reasoning_summary 和 assumptions 的 JSON 对象，不要输出 Markdown 或额外字段。"""


def build_forecast_explanation_prompt(
    *,
    question: str,
    subject_name: str,
    subject_type: str,
    forecast_start_date: date,
    forecast_end_date: date,
    current_stock: int,
    history: list[dict[str, Any]],
    forecast_points: list[ForecastPoint],
    model_version: str,
    recommended_quantity: int,
) -> str:
    """把已生成的模型结果作为只读证据交给 DeepSeek 解释。"""
    response_example = {
        "reasoning_summary": "本地模型预测显示需求总体平稳。",
        "assumptions": ["未来经营条件与历史样本大体一致"],
    }
    return (
        f"用户问题：{question}\n"
        f"预测对象：{subject_name}\n"
        f"对象类型：{subject_type}\n"
        f"数值预测模型：{model_version}\n"
        f"当前库存：{current_stock}\n"
        f"后端确定性补货量：{recommended_quantity}\n"
        f"预测开始日期：{forecast_start_date.isoformat()}\n"
        f"预测结束日期：{forecast_end_date.isoformat()}\n"
        "近期连续历史日销量（缺失成交已补零）：\n"
        f"{json.dumps(history, ensure_ascii=False, default=str)}\n"
        "本地 PyTorch 模型预测（只读，不得修改）：\n"
        f"{json.dumps([point.model_dump(mode='json') for point in forecast_points], ensure_ascii=False)}\n"
        "JSON 结构示例：\n"
        f"{json.dumps(response_example, ensure_ascii=False)}"
    )


class ForecastDeepSeekExplainer:
    """调用 DeepSeek JSON Output 解释本地模型预测。"""

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.http_client = http_client

    async def explain(
        self,
        *,
        question: str,
        subject_name: str,
        subject_type: str,
        forecast_start_date: date,
        forecast_end_date: date,
        current_stock: int,
        history: list[dict[str, Any]],
        forecast_points: list[ForecastPoint],
        model_version: str,
        recommended_quantity: int,
    ) -> ForecastExplanationCompletion:
        """请求严格解释 JSON，并拒绝任何额外预测字段。"""
        if not self.settings.deepseek_api_key:
            raise ConfigurationError("DEEPSEEK_API_KEY is not configured")
        payload: dict[str, Any] = {
            "model": self.settings.deepseek_model,
            "messages": [
                {
                    "role": "system",
                    "content": FORECAST_EXPLANATION_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": build_forecast_explanation_prompt(
                        question=question,
                        subject_name=subject_name,
                        subject_type=subject_type,
                        forecast_start_date=forecast_start_date,
                        forecast_end_date=forecast_end_date,
                        current_stock=current_stock,
                        history=history,
                        forecast_points=forecast_points,
                        model_version=model_version,
                        recommended_quantity=recommended_quantity,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": self.settings.forecast_explanation_temperature,
            "max_tokens": self.settings.forecast_explanation_max_tokens,
            "stream": False,
            "thinking": {
                "type": (
                    "enabled"
                    if self.settings.deepseek_thinking_enabled
                    else "disabled"
                )
            },
        }
        headers = {
            "Authorization": (
                f"Bearer {self.settings.deepseek_api_key.get_secret_value()}"
            ),
            "Content-Type": "application/json",
        }
        url = f"{self.settings.deepseek_base_url.rstrip('/')}/chat/completions"
        try:
            response = await self._post_with_retry(url, headers, payload)
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("forecast completion is not a string")
            result = ForecastExplanation.model_validate(json.loads(content))
            model = str(data.get("model") or self.settings.deepseek_model)
            usage = self._parse_usage(data.get("usage"))
        except ConfigurationError:
            raise
        except (httpx.HTTPError, RetryableDeepSeekError) as exc:
            logger.warning(
                "forecast explanation request failed type=%s",
                type(exc).__name__,
            )
            raise ForecastGenerationError() from exc
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "forecast explanation response invalid type=%s",
                type(exc).__name__,
            )
            raise ForecastGenerationError(
                "DeepSeek returned invalid forecast explanation JSON"
            ) from exc
        return ForecastExplanationCompletion(result=result, model=model, usage=usage)

    async def _post_with_retry(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> httpx.Response:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.settings.deepseek_max_retries + 1),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            retry=retry_if_exception_type(
                (httpx.TransportError, RetryableDeepSeekError)
            ),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                if self.http_client is not None:
                    response = await self.http_client.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=self.settings.deepseek_timeout_seconds,
                    )
                else:
                    async with httpx.AsyncClient() as client:
                        response = await client.post(
                            url,
                            headers=headers,
                            json=payload,
                            timeout=self.settings.deepseek_timeout_seconds,
                        )
                if response.status_code == 429 or response.status_code >= 500:
                    raise RetryableDeepSeekError(
                        f"retryable HTTP status {response.status_code}"
                    )
                response.raise_for_status()
                return response
        raise ForecastGenerationError()

    @staticmethod
    def _parse_usage(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        usage = {
            key: value[key]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if isinstance(value.get(key), int)
        }
        return usage or None
