"""从自然语言或显式 API 参数解析用户指定的预测时间范围。"""

from dataclasses import dataclass
from datetime import date, timedelta
import re

from app.errors import ForecastRequestError


DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})\s*(?:[-/.]|年)\s*"
    r"(?P<month>\d{1,2})\s*(?:[-/.]|月)\s*"
    r"(?P<day>\d{1,2})\s*日?"
)
COUNT_TOKEN = r"(?P<count>\d{1,3}|[零〇一二两三四五六七八九十百]{1,5})"
RELATIVE_PATTERNS = (
    re.compile(rf"(?:未来|接下来|往后|预测)\s*{COUNT_TOKEN}\s*(?P<unit>天|日|周|个月|月)"),
    re.compile(rf"{COUNT_TOKEN}\s*(?P<unit>天|日|周|个月|月)\s*(?:销量|销售|需求|预测|趋势)"),
)


@dataclass(frozen=True, slots=True)
class ForecastWindow:
    """最终可执行的预测窗口及其解析来源。"""

    start_date: date
    end_date: date
    horizon_days: int
    total_steps: int
    source: str


def _parse_chinese_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value == "百":
        return 100
    if "百" in value:
        left, right = value.split("百", 1)
        hundreds = digits.get(left, 1) * 100
        return hundreds + (_parse_chinese_number(right) if right else 0)
    if "十" in value:
        left, right = value.split("十", 1)
        tens = digits.get(left, 1) * 10
        return tens + (digits.get(right, 0) if right else 0)
    result = 0
    for char in value:
        if char not in digits:
            raise ForecastRequestError("unsupported Chinese number")
        result = result * 10 + digits[char]
    return result


def _dates_in_question(question: str) -> list[date]:
    resolved: list[date] = []
    for match in DATE_PATTERN.finditer(question):
        try:
            resolved.append(
                date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            )
        except ValueError as exc:
            raise ForecastRequestError("invalid calendar date") from exc
    return resolved


def _relative_days(question: str) -> int | None:
    for pattern in RELATIVE_PATTERNS:
        match = pattern.search(question)
        if match is None:
            continue
        count = _parse_chinese_number(match.group("count"))
        unit = match.group("unit")
        multiplier = 1 if unit in {"天", "日"} else 7 if unit == "周" else 30
        return count * multiplier
    if "下周" in question:
        return 7
    if "下个月" in question or "下月" in question:
        return 30
    return None


def resolve_forecast_window(
    question: str,
    *,
    data_through: date,
    default_days: int,
    max_days: int,
    explicit_days: int | None = None,
    explicit_start_date: date | None = None,
    explicit_end_date: date | None = None,
) -> ForecastWindow:
    """按显式参数、日期文本、相对天数的优先级解析预测窗口。"""
    if (explicit_start_date is None) != (explicit_end_date is None):
        raise ForecastRequestError("forecast start and end dates must be paired")
    if explicit_days is not None and explicit_start_date is not None:
        raise ForecastRequestError("forecast days and date range cannot be combined")

    source = "default"
    if explicit_start_date is not None and explicit_end_date is not None:
        start_date = explicit_start_date
        end_date = explicit_end_date
        source = "api_date_range"
    elif explicit_days is not None:
        start_date = data_through + timedelta(days=1)
        end_date = data_through + timedelta(days=explicit_days)
        source = "api_days"
    else:
        dates = _dates_in_question(question)
        if len(dates) >= 2:
            start_date, end_date = dates[0], dates[1]
            source = "question_date_range"
        elif len(dates) == 1 and any(
            word in question for word in ("截至", "截止", "预测到", "预估到")
        ):
            start_date = data_through + timedelta(days=1)
            end_date = dates[0]
            source = "question_end_date"
        else:
            days = _relative_days(question) or default_days
            start_date = data_through + timedelta(days=1)
            end_date = data_through + timedelta(days=days)
            source = "question_days" if days != default_days else "default"

    if start_date <= data_through:
        raise ForecastRequestError("forecast must start after the latest actual date")
    if end_date < start_date:
        raise ForecastRequestError("forecast end date precedes start date")
    horizon_days = (end_date - start_date).days + 1
    total_steps = (end_date - data_through).days
    if horizon_days > max_days or total_steps > max_days:
        raise ForecastRequestError("forecast range exceeds configured maximum")
    return ForecastWindow(
        start_date=start_date,
        end_date=end_date,
        horizon_days=horizon_days,
        total_steps=total_steps,
        source=source,
    )
