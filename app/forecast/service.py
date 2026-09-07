"""全局 PyTorch 数值预测、DeepSeek 解释与确定性补货服务。"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from functools import lru_cache
import math

import pandas as pd

from app.config import Settings, get_settings
from app.errors import (
    ConfigurationError,
    ForecastGenerationError,
    ForecastUnavailableError,
)
from app.forecast.data import load_sales_data
from app.forecast.deepseek import ForecastDeepSeekExplainer
from app.forecast.horizon import resolve_forecast_window
from app.forecast.models import (
    ForecastExplanation,
    ReplenishmentSuggestion,
    SalesForecastAnswer,
    SalesForecastResult,
)
from app.forecast.torch_model import TorchForecastPredictor
from app.logging_config import get_logger


logger = get_logger("forecast.service")
DataLoader = Callable[[], Awaitable[tuple[pd.DataFrame, pd.DataFrame]]]


class SalesForecastService:
    """本地模型生成数字，DeepSeek 只解释，补货量由后端计算。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        data_loader: DataLoader | None = None,
        predictor: TorchForecastPredictor | None = None,
        explainer: ForecastDeepSeekExplainer | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.data_loader = data_loader or load_sales_data
        self.predictor = predictor or TorchForecastPredictor(self.settings)
        self.explainer = explainer or ForecastDeepSeekExplainer(self.settings)

    @staticmethod
    def _resolve_subject(
        question: str,
        catalog: pd.DataFrame,
    ) -> tuple[str, str, list[int]]:
        normalized = question.lower()
        categories = sorted(
            catalog["category"].drop_duplicates().astype(str),
            key=len,
            reverse=True,
        )
        if "品类" in question:
            for category in categories:
                if category.lower() in normalized:
                    rows = catalog[catalog["category"] == category]
                    return (
                        "category",
                        category,
                        rows["product_id"].astype(int).tolist(),
                    )

        product_rows = sorted(
            catalog.to_dict("records"),
            key=lambda row: len(str(row["name"])),
            reverse=True,
        )
        for row in product_rows:
            if (
                str(row["name"]).lower() in normalized
                or str(row["sku"]).lower() in normalized
            ):
                return "product", str(row["name"]), [int(row["product_id"])]
        for category in categories:
            if category.lower() in normalized:
                rows = catalog[catalog["category"] == category]
                return (
                    "category",
                    category,
                    rows["product_id"].astype(int).tolist(),
                )
        return "all", "全部在售商品", catalog["product_id"].astype(int).tolist()

    @staticmethod
    def _fallback_explanation(
        subject_name: str,
        model_version: str,
    ) -> ForecastExplanation:
        return ForecastExplanation(
            reasoning_summary=(
                f"{subject_name}的逐日销量及分位数区间由本地全局 PyTorch 模型"
                f" {model_version} 生成。"
            ),
            assumptions=[
                "未来经营条件与历史训练阶段大体一致",
                "未提供的活动、价格和供应变化不会被模型自动感知",
            ],
        )

    async def answer(
        self,
        question: str,
        *,
        forecast_days: int | None = None,
        forecast_start_date: date | None = None,
        forecast_end_date: date | None = None,
    ) -> SalesForecastAnswer:
        """加载已训练模型预测；本方法不会触发任何训练。"""
        history, catalog = await self.data_loader()
        if history.empty or catalog.empty:
            raise ForecastUnavailableError("forecast data is empty")
        data_through = pd.to_datetime(history["sales_date"]).max().date()
        window = resolve_forecast_window(
            question,
            data_through=data_through,
            default_days=self.settings.forecast_default_days,
            max_days=self.settings.forecast_max_days,
            explicit_days=forecast_days,
            explicit_start_date=forecast_start_date,
            explicit_end_date=forecast_end_date,
        )
        subject_type, subject_name, product_ids = self._resolve_subject(
            question,
            catalog,
        )
        selected_catalog = catalog[catalog["product_id"].isin(product_ids)]
        if selected_catalog.empty:
            raise ForecastUnavailableError("forecast subject has no catalog rows")
        current_stock = int(selected_catalog["stock_quantity"].sum())

        selected_history = history[history["product_id"].isin(product_ids)].copy()
        selected_history["sales_date"] = pd.to_datetime(
            selected_history["sales_date"]
        )
        daily_history = (
            selected_history.groupby("sales_date", as_index=False)["quantity"]
            .sum()
            .sort_values("sales_date")
            .tail(self.settings.forecast_history_days)
        )
        if len(daily_history) < self.settings.forecast_min_history_days:
            raise ForecastUnavailableError("forecast history is insufficient")

        completion = await asyncio.to_thread(
            self.predictor.predict,
            history=history,
            catalog=catalog,
            product_ids=product_ids,
            forecast_start_date=window.start_date,
            forecast_end_date=window.end_date,
        )
        points = completion.points
        expected_dates = list(
            pd.date_range(window.start_date, window.end_date, freq="D").date
        )
        if [point.date for point in points] != expected_dates:
            raise ForecastUnavailableError(
                "local forecast model dates do not match requested window"
            )

        total_demand = round(sum(point.predicted_quantity for point in points), 2)
        daily_average = total_demand / window.horizon_days
        safety_stock = math.ceil(
            daily_average * self.settings.forecast_safety_stock_days
        )
        target_stock = math.ceil(total_demand + safety_stock)
        recommended_quantity = max(0, target_stock - current_stock)
        replenishment = ReplenishmentSuggestion(
            current_stock=current_stock,
            forecast_demand=total_demand,
            safety_stock=safety_stock,
            target_stock=target_stock,
            recommended_quantity=recommended_quantity,
            safety_stock_days=self.settings.forecast_safety_stock_days,
        )

        explanation = self._fallback_explanation(
            subject_name,
            completion.model_version,
        )
        explanation_model: str | None = None
        usage: dict[str, int] | None = None
        if self.settings.deepseek_api_key:
            explanation_history = daily_history.tail(
                self.settings.forecast_explanation_history_days
            )
            history_payload = [
                {
                    "date": row.sales_date.date().isoformat(),
                    "quantity": round(float(row.quantity), 2),
                }
                for row in explanation_history.itertuples(index=False)
            ]
            try:
                explained = await self.explainer.explain(
                    question=question,
                    subject_name=subject_name,
                    subject_type=subject_type,
                    forecast_start_date=window.start_date,
                    forecast_end_date=window.end_date,
                    current_stock=current_stock,
                    history=history_payload,
                    forecast_points=points,
                    model_version=completion.model_version,
                    recommended_quantity=recommended_quantity,
                )
                explanation = explained.result
                explanation_model = explained.model
                usage = explained.usage
            except (ConfigurationError, ForecastGenerationError):
                if self.settings.forecast_explanation_required:
                    raise
                logger.warning(
                    "DeepSeek explanation unavailable; using deterministic fallback"
                )
        elif self.settings.forecast_explanation_required:
            raise ConfigurationError("DEEPSEEK_API_KEY is required for explanation")

        historical_daily_average = round(float(daily_history["quantity"].mean()), 2)
        result = SalesForecastResult(
            subject_type=subject_type,
            subject_name=subject_name,
            forecast_start_date=window.start_date,
            forecast_end_date=window.end_date,
            horizon_days=window.horizon_days,
            data_through=data_through,
            points=points,
            total_predicted_quantity=total_demand,
            model_version=completion.model_version,
            generation_method="torch_global",
            prediction_device=completion.prediction_device,
            explanation_model=explanation_model,
            history_start_date=daily_history["sales_date"].min().date(),
            history_days=len(daily_history),
            historical_daily_average=historical_daily_average,
            reasoning_summary=explanation.reasoning_summary,
            replenishment=replenishment,
            assumptions=[
                *explanation.assumptions,
                f"预测窗口来自 {window.source}",
                f"历史销量数据截至 {data_through.isoformat()}",
                f"模型训练数据截至 {completion.trained_through.isoformat()}",
                "上下界为经验证集共形校准的 P10/P90 经验覆盖区间，不等同于参数置信区间",
                "补货量 = 预测需求 + 安全库存 - 当前库存，不考虑在途库存和最小订购量",
            ],
        )
        answer = (
            f"本地全局 PyTorch 模型对{subject_name}在 "
            f"{window.start_date.isoformat()} 至 {window.end_date.isoformat()} 的"
            f"预测销量合计为 {total_demand:,.2f} 件。当前库存 {current_stock} 件，"
            f"建议补货 {recommended_quantity} 件。"
        )
        logger.info(
            "global torch sales forecast completed subject=%s horizon_days=%s "
            "model=%s device=%s",
            subject_name,
            window.horizon_days,
            completion.model_version,
            completion.prediction_device,
        )
        return SalesForecastAnswer(
            answer=answer,
            forecast=result,
            model=completion.model_version,
            usage=usage,
        )


@lru_cache(maxsize=1)
def get_sales_forecast_service() -> SalesForecastService:
    """返回进程内共享的预测服务和模型缓存。"""
    return SalesForecastService()
