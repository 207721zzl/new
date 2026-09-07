"""动态预测区间、特征无泄漏、服务聚合和工作流路由测试。"""

import asyncio
from datetime import date
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from app import graph
from app.config import PROJECT_ROOT, Settings
from app.errors import ForecastRequestError
from app.forecast.horizon import resolve_forecast_window
from app.forecast.models import (
    ForecastExplanation,
    ForecastExplanationCompletion,
    ForecastPoint,
    ReplenishmentSuggestion,
    SalesForecastAnswer,
    SalesForecastResult,
    TorchForecastCompletion,
)
from app.forecast.service import SalesForecastService


DATA_THROUGH = date(2026, 7, 12)


def test_question_controls_forecast_days_and_supports_chinese_numbers():
    window = resolve_forecast_window(
        "预测未来二十一天耳机销量",
        data_through=DATA_THROUGH,
        default_days=7,
        max_days=90,
    )

    assert window.start_date == date(2026, 7, 13)
    assert window.end_date == date(2026, 8, 2)
    assert window.horizon_days == 21
    assert window.source == "question_days"


def test_question_supports_explicit_forecast_date_range():
    window = resolve_forecast_window(
        "预测 2026年7月20日至2026年8月5日的手机销量",
        data_through=DATA_THROUGH,
        default_days=7,
        max_days=90,
    )

    assert window.start_date == date(2026, 7, 20)
    assert window.end_date == date(2026, 8, 5)
    assert window.horizon_days == 17
    assert window.total_steps == 24


def test_explicit_api_days_take_priority_and_maximum_is_enforced():
    window = resolve_forecast_window(
        "预测未来 7 天耳机销量",
        data_through=DATA_THROUGH,
        default_days=7,
        max_days=90,
        explicit_days=35,
    )
    assert window.horizon_days == 35
    assert window.source == "api_days"

    with pytest.raises(ForecastRequestError):
        resolve_forecast_window(
            "预测销量",
            data_through=DATA_THROUGH,
            default_days=7,
            max_days=90,
            explicit_days=91,
        )


def test_forecast_golden_catalog_has_ten_dynamic_horizons():
    cases = json.loads(
        (PROJECT_ROOT / "data" / "evaluation" / "forecast_cases.json").read_text(
            encoding="utf-8"
        )
    )

    assert len(cases) == 10
    for case in cases:
        window = resolve_forecast_window(
            case["question"],
            data_through=DATA_THROUGH,
            default_days=7,
            max_days=90,
        )
        assert window.horizon_days == case["horizon_days"]


class FakeForecastPredictor:
    def predict(self, **kwargs):
        dates = pd.date_range(
            kwargs["forecast_start_date"],
            kwargs["forecast_end_date"],
            freq="D",
        )
        return TorchForecastCompletion(
            points=[
                ForecastPoint(
                    date=value.date(),
                    predicted_quantity=5,
                    lower_bound=4,
                    upper_bound=6,
                )
                for value in dates
            ],
            model_version="torch-global-test",
            prediction_device="cuda:test-gpu",
            trained_through=date(2026, 7, 12),
        )


class FakeForecastExplainer:
    async def explain(self, **kwargs):
        assert kwargs["model_version"] == "torch-global-test"
        return ForecastExplanationCompletion(
            result=ForecastExplanation(
                reasoning_summary="最近销量平稳。",
                assumptions=["经营条件保持稳定"],
            ),
            model="deepseek-test",
            usage={"total_tokens": 321},
        )


async def fake_sales_data():
    dates = pd.date_range("2026-05-14", periods=60, freq="D")
    history = pd.DataFrame({"sales_date": dates, "product_id": 1, "quantity": 4.0})
    catalog = pd.DataFrame(
        [
            {
                "product_id": 1,
                "sku": "EAR-001",
                "name": "降噪耳机",
                "category": "耳机",
                "stock_quantity": 80,
            }
        ]
    )
    return history, catalog


def test_forecast_service_returns_user_horizon_intervals_and_replenishment():
    service = SalesForecastService(
        Settings(
            _env_file=None,
            forecast_default_days=7,
            forecast_max_days=90,
            forecast_safety_stock_days=3,
            deepseek_api_key="test-key",
        ),
        data_loader=fake_sales_data,
        predictor=FakeForecastPredictor(),
        explainer=FakeForecastExplainer(),
    )

    result = asyncio.run(service.answer("预测未来 21 天耳机品类销量"))

    assert result.forecast.horizon_days == 21
    assert len(result.forecast.points) == 21
    assert result.forecast.total_predicted_quantity == 105
    assert result.forecast.subject_type == "category"
    assert result.forecast.replenishment.recommended_quantity == 40
    assert result.forecast.generation_method == "torch_global"
    assert result.forecast.prediction_device == "cuda:test-gpu"
    assert result.forecast.explanation_model == "deepseek-test"
    assert result.forecast.history_days == 60
    assert result.model == "torch-global-test"
    assert result.usage == {"total_tokens": 321}


class FakeForecastService:
    async def answer(self, question, **kwargs):
        assert question == "预测未来 21 天耳机销量"
        assert kwargs["forecast_days"] is None
        forecast = SalesForecastResult(
            subject_type="category",
            subject_name="耳机",
            forecast_start_date=date(2026, 7, 13),
            forecast_end_date=date(2026, 8, 2),
            horizon_days=21,
            data_through=DATA_THROUGH,
            points=[
                ForecastPoint(
                    date=date(2026, 7, 13),
                    predicted_quantity=5,
                    lower_bound=4,
                    upper_bound=6,
                )
            ],
            total_predicted_quantity=105,
            model_version="torch-global-test",
            generation_method="torch_global",
            prediction_device="cuda:test-gpu",
            explanation_model="deepseek-test",
            history_start_date=date(2026, 4, 14),
            history_days=90,
            historical_daily_average=4.5,
            reasoning_summary="历史销量平稳。",
            replenishment=ReplenishmentSuggestion(
                current_stock=80,
                forecast_demand=105,
                safety_stock=15,
                target_stock=120,
                recommended_quantity=40,
                safety_stock_days=3,
            ),
        )
        return SalesForecastAnswer(
            answer="耳机未来 21 天预测销量为 105 件。",
            forecast=forecast,
            model="torch-global-test",
            usage={"total_tokens": 88},
        )


def test_graph_routes_forecast_to_real_forecast_node(monkeypatch):
    class FakeIntentClassifier:
        async def classify(self, question, history_messages=None):
            return SimpleNamespace(
                result=SimpleNamespace(
                    intent="sales_forecast",
                    confidence=0.99,
                    rewritten_question=question,
                    reason="测试预测意图",
                ),
                model="deepseek-intent-test",
                usage={"total_tokens": 20},
            )

    monkeypatch.setattr(
        graph,
        "get_intent_classifier",
        lambda: FakeIntentClassifier(),
    )
    monkeypatch.setattr(
        graph,
        "get_sales_forecast_service",
        lambda: FakeForecastService(),
    )

    result = asyncio.run(
        graph.agent_graph.ainvoke(
            {
                "run_id": "forecast-run",
                "question": "预测未来 21 天耳机销量",
            }
        )
    )

    assert result["intent"] == "sales_forecast"
    assert result["forecast"]["horizon_days"] == 21
    assert result["model"] == "torch-global-test"
    assert result["usage"] == {"total_tokens": 88}
