"""全局 PyTorch 模型文件契约与无训练推理测试。"""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
import pytest
import torch

from app.config import PROJECT_ROOT, Settings
from app.errors import ForecastUnavailableError
from app.forecast.torch_model import (
    FEATURE_NAMES,
    MODEL_FORMAT_VERSION,
    GlobalSalesNetwork,
    TorchForecastPredictor,
    build_feature_vector,
)


def _write_untrained_artifact(path):
    model = GlobalSalesNetwork(
        num_products=1,
        num_categories=1,
        dropout=0,
    )
    for parameter in model.parameters():
        torch.nn.init.zeros_(parameter)
    torch.save(
        {
            "format_version": MODEL_FORMAT_VERSION,
            "model_state_dict": model.state_dict(),
            "model_version": "torch-global-contract-test",
            "trained_through": "2026-07-12",
            "product_id_to_index": {1: 1},
            "category_to_index": {"耳机": 1},
            "architecture": {
                "num_products": 1,
                "num_categories": 1,
                "product_embedding_dim": 8,
                "category_embedding_dim": 4,
                "hidden_sizes": [128, 64],
                "dropout": 0,
            },
            "feature_names": list(FEATURE_NAMES),
            "quantiles": [0.1, 0.5, 0.9],
        },
        path,
    )


def _history_and_catalog():
    dates = pd.date_range("2026-05-14", periods=60, freq="D")
    history = pd.DataFrame(
        {"sales_date": dates, "product_id": 1, "quantity": 4.0}
    )
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


def test_feature_vector_uses_only_prior_values():
    values = [float(value) for value in range(1, 61)]

    features, scale = build_feature_vector(values, date(2026, 7, 13))

    assert len(features) == len(FEATURE_NAMES)
    assert scale == pytest.approx(sum(values[-28:]) / 28)
    assert features[0] == pytest.approx(values[-1] / scale)


def test_predictor_loads_artifact_and_performs_inference_without_training():
    cache_dir = PROJECT_ROOT / ".pytest_cache"
    cache_dir.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=cache_dir) as directory:
        model_path = Path(directory) / "global_torch_forecaster.pt"
        _write_untrained_artifact(model_path)
        history, catalog = _history_and_catalog()
        predictor = TorchForecastPredictor(
            Settings(
                _env_file=None,
                forecast_model_path=model_path,
                forecast_torch_device="cpu",
            )
        )
        result = predictor.predict(
            history=history,
            catalog=catalog,
            product_ids=[1],
            forecast_start_date=date(2026, 7, 13),
            forecast_end_date=date(2026, 7, 15),
        )

        assert result.model_version == "torch-global-contract-test"
        assert result.prediction_device == "cpu"
        assert [point.date for point in result.points] == list(
            pd.date_range("2026-07-13", "2026-07-15", freq="D").date
        )
        assert all(
            point.lower_bound <= point.predicted_quantity <= point.upper_bound
            for point in result.points
        )


def test_predictor_never_trains_when_artifact_is_missing():
    cache_dir = PROJECT_ROOT / ".pytest_cache"
    cache_dir.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=cache_dir) as directory:
        history, catalog = _history_and_catalog()
        predictor = TorchForecastPredictor(
            Settings(
                _env_file=None,
                forecast_model_path=Path(directory) / "missing.pt",
                forecast_torch_device="cpu",
            )
        )

        with pytest.raises(ForecastUnavailableError, match="does not exist"):
            predictor.predict(
                history=history,
                catalog=catalog,
                product_ids=[1],
                forecast_start_date=date(2026, 7, 13),
                forecast_end_date=date(2026, 7, 13),
            )
