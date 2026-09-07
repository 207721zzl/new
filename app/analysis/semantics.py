"""生成 SQL 的业务语义契约校验。"""

from decimal import Decimal, InvalidOperation
from datetime import date, timedelta
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.analysis.models import SQLGenerationResult
from app.db.models import MetricDefinition
from app.errors import Text2SQLGenerationError


REGION_ALIASES = frozenset(
    {"华东区", "华东地区", "华南区", "华南地区", "华北区", "华北地区"}
)


def validate_metric_semantics(
    generation: SQLGenerationResult,
    metric: MetricDefinition,
    question: str = "",
) -> None:
    """拒绝可解析但明显违反区域或指标口径的 SQL。"""
    try:
        statement = sqlglot.parse_one(generation.sql, read="mysql")
    except ParseError:
        return

    _validate_region_literals(statement)
    _validate_explicit_period(statement, question)
    if metric.unit == "%":
        _validate_percentage_scale(statement, generation)
    if metric.metric_code == "refund_amount_rate":
        _validate_refund_period_columns(statement)


def _validate_region_literals(statement: exp.Expression) -> None:
    for predicate in statement.find_all(exp.EQ):
        pairs = ((predicate.left, predicate.right), (predicate.right, predicate.left))
        for column, literal in pairs:
            if (
                isinstance(column, exp.Column)
                and column.name.lower() == "region"
                and isinstance(literal, exp.Literal)
                and literal.is_string
                and literal.this in REGION_ALIASES
            ):
                raise Text2SQLGenerationError(
                    "metric semantics require canonical region values"
                )


def _validate_percentage_scale(
    statement: exp.Expression,
    generation: SQLGenerationResult,
) -> None:
    aliases = {generation.value_column.lower()}
    if generation.analysis_type == "comparison":
        aliases.update(
            column.lower()
            for column in (
                generation.current_value_column,
                generation.previous_value_column,
            )
            if column
        )

    expressions = [
        projection.this if isinstance(projection, exp.Alias) else projection
        for select in statement.find_all(exp.Select)
        for projection in select.expressions
        if projection.alias_or_name.lower() in aliases
    ]
    if not expressions or any(not _contains_numeric_100(item) for item in expressions):
        raise Text2SQLGenerationError(
            "percentage metric must return percentage points"
        )


def _contains_numeric_100(expression: exp.Expression) -> bool:
    for literal in expression.find_all(exp.Literal):
        if literal.is_string:
            continue
        try:
            if Decimal(str(literal.this)) == Decimal(100):
                return True
        except InvalidOperation:
            continue
    return False


def _validate_refund_period_columns(statement: exp.Expression) -> None:
    column_names = {column.name.lower() for column in statement.find_all(exp.Column)}
    if not {"paid_at", "refunded_at"}.issubset(column_names):
        raise Text2SQLGenerationError(
            "refund amount rate requires payment and refund period columns"
        )


ISO_DATE_PATTERN = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
CHINESE_MONTH_PATTERN = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月")


def _validate_explicit_period(statement: exp.Expression, question: str) -> None:
    period = _explicit_period(question)
    if period is None:
        return
    start, end = period
    if not _has_date_boundary(statement, exp.GTE, start):
        raise Text2SQLGenerationError("explicit start date is missing from SQL")
    if not _has_date_boundary(statement, exp.LT, end):
        raise Text2SQLGenerationError("exclusive end date is missing from SQL")


def _explicit_period(question: str) -> tuple[date, date] | None:
    values = [date.fromisoformat(item) for item in ISO_DATE_PATTERN.findall(question)]
    if len(values) >= 2:
        return values[0], values[1] + timedelta(days=1)
    if len(values) == 1:
        return values[0], values[0] + timedelta(days=1)

    match = CHINESE_MONTH_PATTERN.search(question)
    if match is None:
        return None
    start = date(int(match.group(1)), int(match.group(2)), 1)
    if start.month == 12:
        return start, date(start.year + 1, 1, 1)
    return start, date(start.year, start.month + 1, 1)


def _has_date_boundary(
    statement: exp.Expression,
    comparison_type: type[exp.Expression],
    expected: date,
) -> bool:
    expected_text = expected.isoformat()
    for comparison in statement.find_all(comparison_type):
        literal = comparison.right
        if (
            isinstance(literal, exp.Literal)
            and literal.is_string
            and str(literal.this).startswith(expected_text)
        ):
            return True
    return False
