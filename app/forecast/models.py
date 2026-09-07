"""全局 PyTorch 销量预测与 DeepSeek 解释的输入输出模型。"""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ForecastSubjectType = Literal["product", "category", "all"]


class ForecastPoint(BaseModel):
    """单日销量中位数预测及分位数区间。"""

    date: date
    predicted_quantity: float = Field(ge=0)
    lower_bound: float = Field(ge=0)
    upper_bound: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_bounds(self):
        """确保分位数上下界包含中位数预测。"""
        if not self.lower_bound <= self.predicted_quantity <= self.upper_bound:
            raise ValueError("forecast bounds must contain predicted quantity")
        return self


class ForecastExplanation(BaseModel):
    """DeepSeek 只能返回的解释文本，不允许生成预测数字。"""

    model_config = ConfigDict(extra="forbid")

    reasoning_summary: str = Field(min_length=1, max_length=1000)
    assumptions: list[str] = Field(default_factory=list, max_length=10)


class ReplenishmentSuggestion(BaseModel):
    """由预测需求和库存快照确定性计算的补货建议。"""

    current_stock: int = Field(ge=0)
    forecast_demand: float = Field(ge=0)
    safety_stock: int = Field(ge=0)
    target_stock: int = Field(ge=0)
    recommended_quantity: int = Field(ge=0)
    safety_stock_days: int = Field(ge=0)


class SalesForecastResult(BaseModel):
    """可直接由 API 和页面渲染的销量预测结果。"""

    subject_type: ForecastSubjectType
    subject_name: str
    forecast_start_date: date
    forecast_end_date: date
    horizon_days: int = Field(ge=1)
    data_through: date
    points: list[ForecastPoint]
    total_predicted_quantity: float = Field(ge=0)
    model_version: str
    generation_method: Literal["torch_global"] = "torch_global"
    prediction_device: str
    explanation_model: str | None = None
    history_start_date: date
    history_days: int = Field(ge=1)
    historical_daily_average: float = Field(ge=0)
    reasoning_summary: str
    replenishment: ReplenishmentSuggestion
    assumptions: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SalesForecastAnswer:
    """预测服务返回给 LangGraph 的聚合结果。"""

    answer: str
    forecast: SalesForecastResult
    model: str
    usage: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class ForecastExplanationCompletion:
    """DeepSeek 解释及上游调用元数据。"""

    result: ForecastExplanation
    model: str
    usage: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class TorchForecastCompletion:
    """本地全局 PyTorch 模型输出的逐日分位数预测。"""

    points: list[ForecastPoint]
    model_version: str
    prediction_device: str
    trained_through: date
