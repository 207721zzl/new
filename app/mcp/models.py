"""FastMCP 工具的显式 Pydantic 输入输出契约。"""

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.forecast.models import SalesForecastResult


class TableSchemaRequest(BaseModel):
    """查询全部白名单表，或只查询一个指定表。"""

    model_config = ConfigDict(extra="forbid")

    table_name: str | None = Field(default=None, max_length=64)


class TableColumnResult(BaseModel):
    """工具可见的单个字段定义。"""

    name: str
    data_type: str
    description: str


class TableSchemaResult(BaseModel):
    """工具可见的单表定义。"""

    name: str
    description: str
    columns: list[TableColumnResult]


class TableSchemaResponse(BaseModel):
    """表结构查询工具响应。"""

    tables: list[TableSchemaResult]


class ReadOnlySQLRequest(BaseModel):
    """需要通过统一安全边界执行的单条只读 SQL。"""

    model_config = ConfigDict(extra="forbid")

    sql: str = Field(min_length=1, max_length=50_000)


class ReadOnlySQLResponse(BaseModel):
    """经 AST 校验、EXPLAIN 和只读账号执行后的结果。"""

    sql: str
    tables: list[str]
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int = Field(ge=0)
    explain: list[dict[str, Any]]
    execution_user: str
    timeout_ms: int = Field(gt=0)


class SalesForecastToolRequest(BaseModel):
    """销量预测工具输入，沿用 API 的动态日期契约。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2_000)
    forecast_days: int | None = Field(default=None, ge=1, le=365)
    forecast_start_date: date | None = None
    forecast_end_date: date | None = None

    @model_validator(mode="after")
    def validate_explicit_window(self):
        """显式日期必须成对提供，且不能同时提供天数。"""
        has_start = self.forecast_start_date is not None
        has_end = self.forecast_end_date is not None
        if has_start != has_end:
            raise ValueError(
                "forecast_start_date and forecast_end_date must be provided together"
            )
        if self.forecast_days is not None and has_start:
            raise ValueError(
                "forecast_days cannot be combined with an explicit date range"
            )
        return self


class SalesForecastToolResponse(BaseModel):
    """包含结构化预测、模型版本与 Token 用量的工具响应。"""

    answer: str
    forecast: SalesForecastResult
    model: str
    usage: dict[str, int] | None = None
