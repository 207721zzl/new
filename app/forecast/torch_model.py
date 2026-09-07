"""加载全局 PyTorch 销量模型并执行无训练的递归分位数预测。"""

from __future__ import annotations

from datetime import date, timedelta
import math
from pathlib import Path
from threading import Lock
from typing import Any

import pandas as pd
import torch
from torch import Tensor, nn
from torch.nn import functional as F

from app.config import Settings, get_settings
from app.errors import ForecastUnavailableError
from app.forecast.models import ForecastPoint, TorchForecastCompletion
from app.logging_config import get_logger


logger = get_logger("forecast.torch")

MODEL_FORMAT_VERSION = 1
LAGS = (1, 2, 3, 6, 7, 14, 21, 28)
ROLLING_WINDOWS = (7, 14, 28)
FEATURE_NAMES = (
    *(f"lag_{lag}_scaled" for lag in LAGS),
    *(name for window in ROLLING_WINDOWS for name in (f"mean_{window}_scaled", f"std_{window}_scaled")),
    "weekday_sin",
    "weekday_cos",
    "year_sin",
    "year_cos",
    "is_weekend",
    "log1p_scale",
)


class GlobalSalesNetwork(nn.Module):
    """跨商品共享参数，并用商品与品类嵌入区分不同销量序列。"""

    def __init__(
        self,
        *,
        num_products: int,
        num_categories: int,
        feature_count: int = len(FEATURE_NAMES),
        product_embedding_dim: int = 8,
        category_embedding_dim: int = 4,
        hidden_sizes: tuple[int, int] = (128, 64),
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        self.product_embedding = nn.Embedding(
            num_products + 1,
            product_embedding_dim,
        )
        self.category_embedding = nn.Embedding(
            num_categories + 1,
            category_embedding_dim,
        )
        input_size = feature_count + product_embedding_dim + category_embedding_dim
        self.network = nn.Sequential(
            nn.Linear(input_size, hidden_sizes[0]),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_sizes[0], hidden_sizes[1]),
            nn.ReLU(),
            nn.Linear(hidden_sizes[1], 3),
        )

    def forward(
        self,
        features: Tensor,
        product_indices: Tensor,
        category_indices: Tensor,
    ) -> Tensor:
        """返回天然满足 lower <= median <= upper 的三个非负分位数。"""
        combined = torch.cat(
            [
                features,
                self.product_embedding(product_indices),
                self.category_embedding(category_indices),
            ],
            dim=1,
        )
        raw = self.network(combined)
        lower = F.softplus(raw[:, 0])
        median = lower + F.softplus(raw[:, 1])
        upper = median + F.softplus(raw[:, 2])
        return torch.stack([lower, median, upper], dim=1)


def build_feature_vector(values: list[float], target_date: date) -> tuple[list[float], float]:
    """只使用目标日之前的销量构造无泄漏时序特征。"""
    if len(values) < max(LAGS):
        raise ForecastUnavailableError("at least 28 history days are required")
    scale = max(1.0, sum(values[-28:]) / 28)
    features = [float(values[-lag]) / scale for lag in LAGS]
    for window in ROLLING_WINDOWS:
        tail = values[-window:]
        mean = sum(tail) / window
        variance = sum((value - mean) ** 2 for value in tail) / window
        features.extend([mean / scale, math.sqrt(variance) / scale])
    weekday_angle = 2 * math.pi * target_date.weekday() / 7
    year_length = 366 if target_date.replace(month=12, day=31).timetuple().tm_yday == 366 else 365
    year_angle = 2 * math.pi * (target_date.timetuple().tm_yday - 1) / year_length
    features.extend(
        [
            math.sin(weekday_angle),
            math.cos(weekday_angle),
            math.sin(year_angle),
            math.cos(year_angle),
            float(target_date.weekday() >= 5),
            math.log1p(scale),
        ]
    )
    return features, scale


class TorchForecastPredictor:
    """延迟加载已训练模型文件；本类不会启动训练。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = Lock()
        self._model: GlobalSalesNetwork | None = None
        self._device: torch.device | None = None
        self._artifact: dict[str, Any] | None = None

    def _resolve_device(self) -> torch.device:
        requested = self.settings.forecast_torch_device
        if requested == "cuda":
            if not torch.cuda.is_available():
                raise ForecastUnavailableError("CUDA forecast device is unavailable")
            return torch.device("cuda")
        if requested == "cpu":
            return torch.device("cpu")
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")

    def _load_artifact(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise ForecastUnavailableError(
                f"forecast model artifact does not exist: {path}"
            )
        try:
            artifact = torch.load(path, map_location="cpu", weights_only=True)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ForecastUnavailableError("failed to load forecast model artifact") from exc
        if not isinstance(artifact, dict):
            raise ForecastUnavailableError("forecast model artifact must be a mapping")
        required = {
            "format_version",
            "model_state_dict",
            "model_version",
            "trained_through",
            "product_id_to_index",
            "category_to_index",
            "architecture",
            "feature_names",
            "quantiles",
        }
        if missing := required.difference(artifact):
            raise ForecastUnavailableError(
                f"forecast model artifact is missing fields: {sorted(missing)}"
            )
        if int(artifact["format_version"]) != MODEL_FORMAT_VERSION:
            raise ForecastUnavailableError("forecast model artifact version is unsupported")
        if tuple(artifact["feature_names"]) != FEATURE_NAMES:
            raise ForecastUnavailableError("forecast feature contract does not match artifact")
        if [float(value) for value in artifact["quantiles"]] != [0.1, 0.5, 0.9]:
            raise ForecastUnavailableError("forecast artifact quantiles must be P10/P50/P90")
        return artifact

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            artifact = self._load_artifact(self.settings.resolved_forecast_model_path)
            architecture = artifact["architecture"]
            try:
                hidden_sizes = tuple(int(value) for value in architecture["hidden_sizes"])
                if len(hidden_sizes) != 2:
                    raise ValueError("hidden_sizes must contain two values")
                model = GlobalSalesNetwork(
                    num_products=int(architecture["num_products"]),
                    num_categories=int(architecture["num_categories"]),
                    feature_count=len(FEATURE_NAMES),
                    product_embedding_dim=int(architecture["product_embedding_dim"]),
                    category_embedding_dim=int(architecture["category_embedding_dim"]),
                    hidden_sizes=(hidden_sizes[0], hidden_sizes[1]),
                    dropout=float(architecture["dropout"]),
                )
                model.load_state_dict(artifact["model_state_dict"], strict=True)
                device = self._resolve_device()
                model.to(device)
                model.eval()
            except (KeyError, RuntimeError, TypeError, ValueError) as exc:
                raise ForecastUnavailableError(
                    "forecast model artifact is incompatible"
                ) from exc
            self._artifact = artifact
            self._device = device
            self._model = model
            logger.info(
                "global torch forecast model loaded version=%s device=%s",
                artifact["model_version"],
                device,
            )

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def predict(
        self,
        *,
        history: pd.DataFrame,
        catalog: pd.DataFrame,
        product_ids: list[int],
        forecast_start_date: date,
        forecast_end_date: date,
    ) -> TorchForecastCompletion:
        """用已训练模型递归预测每个商品，再聚合所选对象。"""
        self._ensure_loaded()
        assert self._model is not None
        assert self._device is not None
        assert self._artifact is not None

        normalized_history = history.copy()
        normalized_history["sales_date"] = pd.to_datetime(
            normalized_history["sales_date"]
        )
        data_through = normalized_history["sales_date"].max().date()
        if forecast_start_date <= data_through:
            raise ForecastUnavailableError("forecast must start after history data")
        selected_catalog = catalog[catalog["product_id"].isin(product_ids)].copy()
        if len(selected_catalog) != len(set(product_ids)):
            raise ForecastUnavailableError("forecast product is missing from catalog")

        product_map = {
            int(key): int(value)
            for key, value in self._artifact["product_id_to_index"].items()
        }
        sku_map = {
            str(key): int(value)
            for key, value in self._artifact.get("sku_to_index", {}).items()
        }
        category_map = {
            str(key): int(value)
            for key, value in self._artifact["category_to_index"].items()
        }
        aggregate: dict[date, list[float]] = {}
        final_step = (forecast_end_date - data_through).days
        with self._lock, torch.inference_mode():
            for row in selected_catalog.itertuples(index=False):
                product_id = int(row.product_id)
                category = str(row.category)
                product_history = (
                    normalized_history[
                        normalized_history["product_id"] == product_id
                    ]
                    .sort_values("sales_date")["quantity"]
                    .astype(float)
                    .tolist()
                )
                if len(product_history) < self.settings.forecast_min_history_days:
                    raise ForecastUnavailableError(
                        "forecast product has insufficient history"
                    )
                product_index = sku_map.get(
                    str(row.sku),
                    product_map.get(product_id, 0),
                )
                category_index = category_map.get(category, 0)
                for offset in range(1, final_step + 1):
                    target_date = data_through + timedelta(days=offset)
                    features, scale = build_feature_vector(product_history, target_date)
                    feature_tensor = torch.tensor(
                        [features],
                        dtype=torch.float32,
                        device=self._device,
                    )
                    product_tensor = torch.tensor(
                        [product_index],
                        dtype=torch.long,
                        device=self._device,
                    )
                    category_tensor = torch.tensor(
                        [category_index],
                        dtype=torch.long,
                        device=self._device,
                    )
                    quantiles = (
                        self._model(
                            feature_tensor,
                            product_tensor,
                            category_tensor,
                        )[0]
                        .detach()
                        .cpu()
                        .tolist()
                    )
                    lower, median, upper = [
                        max(0.0, float(value) * scale) for value in quantiles
                    ]
                    interval_expansion = max(
                        0.0,
                        float(
                            self._artifact.get("interval_calibration_scaled", 0.0)
                        )
                        * scale,
                    )
                    lower = max(0.0, lower - interval_expansion)
                    upper += interval_expansion
                    product_history.append(median)
                    if target_date >= forecast_start_date:
                        totals = aggregate.setdefault(target_date, [0.0, 0.0, 0.0])
                        totals[0] += lower
                        totals[1] += median
                        totals[2] += upper

        expected_dates = list(
            pd.date_range(forecast_start_date, forecast_end_date, freq="D").date
        )
        if list(aggregate) != expected_dates:
            raise ForecastUnavailableError("forecast model returned incomplete dates")
        points = [
            ForecastPoint(
                date=forecast_date,
                lower_bound=round(aggregate[forecast_date][0], 2),
                predicted_quantity=round(aggregate[forecast_date][1], 2),
                upper_bound=round(aggregate[forecast_date][2], 2),
            )
            for forecast_date in expected_dates
        ]
        device_name = str(self._device)
        if self._device.type == "cuda":
            device_name = f"cuda:{torch.cuda.get_device_name(self._device)}"
        return TorchForecastCompletion(
            points=points,
            model_version=str(self._artifact["model_version"]),
            prediction_device=device_name,
            trained_through=date.fromisoformat(str(self._artifact["trained_through"])),
        )
