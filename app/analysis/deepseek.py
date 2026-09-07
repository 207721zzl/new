"""DeepSeek 结构化 Text2SQL 客户端与受约束 Prompt。"""

import json
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_exponential

from app.analysis.models import (
    AnalysisType,
    SQLGenerationResult,
    Text2SQLCompletion,
    Text2SQLRepairContext,
)
from app.analysis.schema import render_schema_description
from app.config import Settings, get_settings
from app.db.models import MetricDefinition
from app.errors import ConfigurationError, Text2SQLGenerationError
from app.logging_config import get_logger
from app.rag.deepseek import RetryableDeepSeekError


logger = get_logger("analysis.deepseek")

TEXT2SQL_SYSTEM_PROMPT = """你是经营分析系统中的 MySQL 8 Text2SQL 生成器。
你只能依据用户问题、给定业务 Schema 和指标定义生成查询。
只生成一条 SELECT 或 WITH ... SELECT；禁止任何写操作、DDL、系统表、多语句、注释指令和危险函数。
必须使用明确的表别名与字段名，不使用 SELECT *，金额聚合使用 COALESCE。
时间区间使用左闭右开；“上周”使用 Prompt 给出的完整自然周边界。
用户明确给出日期时不得改写为周或月：单日 YYYY-MM-DD 使用 [当天, 次日)，起止日期使用 [起日, 终日次日)。
“环比”比较紧邻的上一等长周期；“同比”比较上一年度的同一日历周期。
区域维度只允许使用标准值“华东”“华南”“华北”；用户说“华东区”“华东地区”等时必须去掉“区/地区”后再过滤。
单位为“%”的指标必须返回百分数而不是小数比例，SQL 计算结果必须乘以 100。
退款金额率的分子按 refund_status = 'success' 和 refunded_at 归属统计周期，分母按 payment_status = 'success' 和 paid_at 归属同一统计周期。
结果必须是 JSON 对象，且只能包含 sql、metric_code、value_column、analysis_type、current_value_column、previous_value_column、current_period、previous_period、assumptions。
普通指标查询的 analysis_type 为 metric；双周期对比查询为 comparison，并使用条件聚合返回唯一一行。
comparison 查询必须分别用 current_value_column 和 previous_value_column 指定两个数值列，禁止让模型计算增长率。
sql 的主数值结果必须使用 value_column 指定的英文别名。不要使用 Markdown 代码块。"""


REGION_SUFFIX_PATTERN = re.compile(r"(华[东南北])(?:地区|区)")


def normalize_business_terms(question: str) -> str:
    """把用户表达映射为数据库采用的稳定业务维度值。"""
    return REGION_SUFFIX_PATTERN.sub(r"\1", question)


def _last_complete_week(today: date) -> tuple[date, date]:
    current_week_start = today - timedelta(days=today.weekday())
    return current_week_start - timedelta(days=7), current_week_start


def build_text2sql_prompt(
    question: str,
    metrics: list[MetricDefinition],
    *,
    analysis_type: AnalysisType = "metric",
    today: date | None = None,
    repair_context: Text2SQLRepairContext | None = None,
) -> str:
    """注入白名单 Schema、活跃指标定义与确定性日期边界。"""
    resolved_today = today or date.today()
    last_week_start, current_week_start = _last_complete_week(resolved_today)
    previous_week_start = last_week_start - timedelta(days=7)
    current_month_start = resolved_today.replace(day=1)
    last_month_start = (current_month_start - timedelta(days=1)).replace(day=1)
    previous_month_start = (last_month_start - timedelta(days=1)).replace(day=1)
    last_year_month_start = last_month_start.replace(year=last_month_start.year - 1)
    last_year_month_end = current_month_start.replace(year=current_month_start.year - 1)
    metric_payload = [metric.as_dict() for metric in metrics]
    comparison_instruction = ""
    response_example = (
        '{"sql":"SELECT ... AS gmv ...","metric_code":"gmv",'
        '"value_column":"gmv","analysis_type":"metric",'
        '"assumptions":["上周按自然周计算"]}'
    )
    if analysis_type == "comparison":
        comparison_instruction = (
            "本题必须生成 comparison 查询：使用 CASE WHEN 条件聚合，在唯一一行中"
            "同时返回 current_value 和 previous_value；SQL 只读取覆盖两个周期的时间范围；"
            "不要在 SQL 中计算差值或增长率。\n"
        )
        response_example = (
            '{"sql":"SELECT COALESCE(SUM(CASE WHEN ... THEN amount ELSE 0 END),0) '
            'AS current_value, COALESCE(SUM(CASE WHEN ... THEN amount ELSE 0 END),0) '
            'AS previous_value FROM ...","metric_code":"gmv",'
            '"value_column":"current_value","analysis_type":"comparison",'
            '"current_value_column":"current_value",'
            '"previous_value_column":"previous_value",'
            f'"current_period":"{last_week_start.isoformat()} 至 '
            f'{current_week_start.isoformat()}",'
            f'"previous_period":"{previous_week_start.isoformat()} 至 '
            f'{last_week_start.isoformat()}",'
            '"assumptions":["按完整自然周比较"]}'
        )
    repair_instruction = ""
    if repair_context is not None:
        previous_sql = repair_context.previous_sql or "上一次响应未形成可解析 SQL"
        repair_instruction = (
            "\n这是自动修复请求，必须根据失败反馈重新生成完整 JSON，不要解释。\n"
            f"修复序号：{repair_context.attempt}\n"
            f"失败类型：{repair_context.failure_type}\n"
            f"上一次 SQL：{previous_sql}\n"
            f"修复要求：{repair_context.instruction}\n"
        )
    return (
        f"查询基准日期：{resolved_today.isoformat()}\n"
        "上周定义（完整自然周，左闭右开）："
        f"[{last_week_start.isoformat()} 00:00:00, "
        f"{current_week_start.isoformat()} 00:00:00)\n"
        "前一周定义（完整自然周，左闭右开）："
        f"[{previous_week_start.isoformat()} 00:00:00, "
        f"{last_week_start.isoformat()} 00:00:00)\n"
        "上月定义（完整自然月，左闭右开）："
        f"[{last_month_start.isoformat()} 00:00:00, "
        f"{current_month_start.isoformat()} 00:00:00)\n"
        "上月的前一月定义（环比）："
        f"[{previous_month_start.isoformat()} 00:00:00, "
        f"{last_month_start.isoformat()} 00:00:00)\n"
        "上月去年同期定义（同比）："
        f"[{last_year_month_start.isoformat()} 00:00:00, "
        f"{last_year_month_end.isoformat()} 00:00:00)\n"
        f"要求的分析类型：{analysis_type}\n"
        f"{comparison_instruction}\n"
        f"允许的业务 Schema 与字段白名单：\n{render_schema_description()}\n\n"
        "当前启用的 metric_definitions：\n"
        f"{json.dumps(metric_payload, ensure_ascii=False, default=str)}\n\n"
        f"用户问题：{normalize_business_terms(question)}\n\n"
        f"{repair_instruction}"
        f"返回 JSON 示例：{response_example}"
    )


class Text2SQLClient:
    """调用 DeepSeek JSON Output 生成可校验的 SQL 对象。"""

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
        *,
        today_provider: Callable[[], date] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.http_client = http_client
        self.today_provider = today_provider or (
            lambda: datetime.now(ZoneInfo(self.settings.business_timezone)).date()
        )

    async def generate(
        self,
        question: str,
        metrics: list[MetricDefinition],
        *,
        analysis_type: AnalysisType = "metric",
        repair_context: Text2SQLRepairContext | None = None,
    ) -> Text2SQLCompletion:
        """请求结构化结果，并用 Pydantic 严格校验响应字段。"""
        if not self.settings.deepseek_api_key:
            raise ConfigurationError("DEEPSEEK_API_KEY is not configured")
        if not metrics:
            raise Text2SQLGenerationError("no active metric definitions")

        payload: dict[str, Any] = {
            "model": self.settings.deepseek_model,
            "messages": [
                {"role": "system", "content": TEXT2SQL_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_text2sql_prompt(
                        question,
                        metrics,
                        analysis_type=analysis_type,
                        today=self.today_provider(),
                        repair_context=repair_context,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": self.settings.deepseek_max_tokens,
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
                raise TypeError("completion content is not a string")
            generation = SQLGenerationResult.model_validate(json.loads(content))
            model = str(data.get("model") or self.settings.deepseek_model)
            usage = self._parse_usage(data.get("usage"))
        except ConfigurationError:
            raise
        except (httpx.HTTPError, RetryableDeepSeekError) as exc:
            logger.warning("Text2SQL request failed error_type=%s", type(exc).__name__)
            raise Text2SQLGenerationError(repairable=False) from exc
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "Text2SQL response invalid error_type=%s", type(exc).__name__
            )
            raise Text2SQLGenerationError("DeepSeek returned invalid Text2SQL JSON") from exc

        return Text2SQLCompletion(result=generation, model=model, usage=usage)

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
        raise Text2SQLGenerationError()

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
