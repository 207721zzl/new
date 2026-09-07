"""Text2SQL 生成、执行与 API 证据模型。"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from app.db.models import MetricDefinition


AnalysisType = Literal["metric", "comparison"]
RootCauseDimension = Literal["region", "category", "product"]


class SQLGenerationResult(BaseModel):
    """DeepSeek 必须返回的结构化 SQL 生成结果。"""

    model_config = ConfigDict(extra="forbid")

    sql: str = Field(min_length=1)
    metric_code: str = Field(min_length=1, max_length=64)
    value_column: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    analysis_type: AnalysisType = "metric"
    current_value_column: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$",
    )
    previous_value_column: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$",
    )
    current_period: str | None = Field(default=None, max_length=128)
    previous_period: str | None = Field(default=None, max_length=128)
    assumptions: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_comparison_columns(self) -> Self:
        """对比查询必须显式声明两个可验证的数值列。"""
        if self.analysis_type == "comparison" and (
            not self.current_value_column or not self.previous_value_column
        ):
            raise ValueError(
                "comparison requires current_value_column and previous_value_column"
            )
        return self


@dataclass(frozen=True, slots=True)
class Text2SQLCompletion:
    """结构化 SQL 及 DeepSeek 调用元数据。"""

    result: SQLGenerationResult
    model: str
    usage: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class Text2SQLRepairContext:
    """下一次生成可见的稳定修复反馈，不包含底层连接或异常细节。"""

    attempt: int
    failure_type: Literal["contract_error", "unsafe_sql", "execution_error"]
    instruction: str
    previous_sql: str | None = None


@dataclass(frozen=True, slots=True)
class ValidatedSQL:
    """通过 SQLGlot 白名单校验后可交给执行器的 SQL。"""

    sql: str
    tables: tuple[str, ...]
    limit: int


@dataclass(frozen=True, slots=True)
class QueryExecutionResult:
    """只读执行器返回的内部结果。"""

    columns: list[str]
    rows: list[dict[str, Any]]
    explain: list[dict[str, Any]]


class MetricEvidence(BaseModel):
    """回答采用的指标口径快照。"""

    code: str
    name: str
    description: str
    formula: str
    unit: str
    version: str


class QueryEvidence(BaseModel):
    """支持分析结论的可审计数据证据。"""

    metric: MetricEvidence
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int = Field(ge=0)
    explain: list[dict[str, Any]]
    tables: list[str]
    execution_user: str
    timeout_ms: int = Field(gt=0)
    assumptions: list[str] = Field(default_factory=list)


class DataAnalysisResult(BaseModel):
    """通用运行接口返回的数据分析闭环结果。"""

    value: int | float
    unit: str
    sql: str
    evidence: QueryEvidence
    comparison: "ComparisonAnalysis | None" = None
    root_cause: "RootCauseAnalysis | None" = None
    repair_count: int = Field(default=0, ge=0, le=2)


class ComparisonAnalysis(BaseModel):
    """完全由查询结果确定性计算的双周期比较。"""

    current_value: int | float
    previous_value: int | float
    change: int | float
    change_rate: float | None
    direction: Literal["increase", "decrease", "flat"]
    current_period: str
    previous_period: str


class RootCauseItem(BaseModel):
    """单个维度成员对总体变动的确定性贡献。"""

    dimension: RootCauseDimension
    name: str
    current_value: int | float
    previous_value: int | float
    change: int | float
    change_rate: float | None
    contribution_rate: float | None
    direction: Literal["increase", "decrease", "flat"]
    is_driver: bool
    is_anomaly: bool


class RootCauseBreakdown(BaseModel):
    """一个业务维度的拆解结果、SQL 和可审计证据。"""

    dimension: RootCauseDimension
    label: str
    sql: str
    evidence: QueryEvidence
    items: list[RootCauseItem]
    reconciled: bool
    current_reconciliation_error: float
    previous_reconciliation_error: float


class RootCauseAnalysis(BaseModel):
    """跨区域、品类和商品的原因分析结果。"""

    scope: list[str] = Field(default_factory=list)
    breakdowns: list[RootCauseBreakdown]
    primary_drivers: list[RootCauseItem]
    anomaly_count: int = Field(ge=0)
    limitations: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DataAnalysisAnswer:
    """数据分析服务返回给 LangGraph 的聚合结果。"""

    answer: str
    analysis: DataAnalysisResult
    model: str | None
    usage: dict[str, int] | None


@dataclass(frozen=True, slots=True)
class DataAnalysisAttemptResult:
    """一次通过校验并完成只读执行的 Text2SQL 图节点结果。"""

    completion: Text2SQLCompletion
    metric: "MetricDefinition"
    validated: ValidatedSQL
    execution: QueryExecutionResult
    value: int | float
    comparison: ComparisonAnalysis | None
