"""Offline training for the global daily sales quantile network."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import math
from pathlib import Path
import random
from typing import Any

import numpy as np
import pandas as pd
import torch
from torch import Tensor
from torch.utils.data import DataLoader, TensorDataset

from app.forecast.torch_model import (
    FEATURE_NAMES,
    MODEL_FORMAT_VERSION,
    GlobalSalesNetwork,
    build_feature_vector,
)


HARD_SAMPLE_LIMIT = 2_000
QUANTILES = (0.1, 0.5, 0.9)
UNKNOWN_CATEGORY = "__unknown__"


@dataclass(frozen=True, slots=True)
class DailySalesAudit:
    source_rows: int
    valid_rows: int
    daily_rows: int
    unique_skus: int
    start_date: date
    end_date: date
    rejected_unfinished_rows: int
    rejected_non_positive_rows: int
    rejected_invalid_rows: int


@dataclass(frozen=True, slots=True)
class SampleSplit:
    name: str
    features: np.ndarray
    targets_scaled: np.ndarray
    targets: np.ndarray
    scales: np.ndarray
    product_indices: np.ndarray
    category_indices: np.ndarray
    target_dates: np.ndarray
    sku_ids: np.ndarray
    lag7_baseline: np.ndarray
    mean28_baseline: np.ndarray
    candidate_count: int
    positive_candidate_count: int

    @property
    def size(self) -> int:
        return int(self.targets.shape[0])


@dataclass(frozen=True, slots=True)
class PreparedSamples:
    train: SampleSplit
    validation: SampleSplit
    test: SampleSplit
    sku_to_index: dict[int, int]
    category_to_index: dict[str, int]
    source_start_date: date
    source_end_date: date

    @property
    def total_size(self) -> int:
        return self.train.size + self.validation.size + self.test.size


def load_daily_sales(csv_path: Path) -> tuple[pd.DataFrame, DailySalesAudit]:
    """Load transactions and aggregate valid completed quantities to SKU-days."""
    required = {"create_dt", "sku_id", "is_finished", "sku_cnt"}
    frame = pd.read_csv(
        csv_path,
        usecols=lambda column: column in required,
        encoding="utf-8",
        encoding_errors="replace",
        low_memory=False,
    )
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"sales CSV is missing columns: {sorted(missing)}")

    source_rows = len(frame)
    frame["sales_date"] = pd.to_datetime(frame["create_dt"], errors="coerce").dt.normalize()
    frame["sku_id"] = pd.to_numeric(frame["sku_id"], errors="coerce")
    frame["is_finished"] = pd.to_numeric(frame["is_finished"], errors="coerce")
    frame["quantity"] = pd.to_numeric(frame["sku_cnt"], errors="coerce")

    invalid_mask = frame[["sales_date", "sku_id", "is_finished", "quantity"]].isna().any(axis=1)
    unfinished_mask = frame["is_finished"].ne(1) & ~invalid_mask
    non_positive_mask = frame["quantity"].le(0) & ~invalid_mask & ~unfinished_mask
    valid = frame.loc[
        ~invalid_mask & ~unfinished_mask & ~non_positive_mask,
        ["sales_date", "sku_id", "quantity"],
    ].copy()
    if valid.empty:
        raise ValueError("sales CSV has no valid completed positive rows")
    valid["sku_id"] = valid["sku_id"].astype("int64")
    valid["quantity"] = valid["quantity"].astype("float32")
    daily = (
        valid.groupby(["sku_id", "sales_date"], as_index=False, sort=True)["quantity"]
        .sum()
        .sort_values(["sales_date", "sku_id"])
        .reset_index(drop=True)
    )
    audit = DailySalesAudit(
        source_rows=source_rows,
        valid_rows=len(valid),
        daily_rows=len(daily),
        unique_skus=int(daily["sku_id"].nunique()),
        start_date=daily["sales_date"].min().date(),
        end_date=daily["sales_date"].max().date(),
        rejected_unfinished_rows=int(unfinished_mask.sum()),
        rejected_non_positive_rows=int(non_positive_mask.sum()),
        rejected_invalid_rows=int(invalid_mask.sum()),
    )
    return daily, audit


def load_category_map(catalog_path: Path | None) -> dict[int, str]:
    """Read optional SKU categories; training remains valid without this file."""
    if catalog_path is None:
        return {}
    frame = pd.read_excel(
        catalog_path,
        sheet_name="sku_category",
        usecols=["sku_id", "third_category_name"],
    )
    frame["sku_id"] = pd.to_numeric(frame["sku_id"], errors="coerce")
    frame["third_category_name"] = frame["third_category_name"].fillna(UNKNOWN_CATEGORY)
    frame = frame.dropna(subset=["sku_id"]).drop_duplicates("sku_id", keep="first")
    return {
        int(row.sku_id): str(row.third_category_name).strip() or UNKNOWN_CATEGORY
        for row in frame.itertuples(index=False)
    }


def _dense_daily_matrix(
    daily: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, pd.DatetimeIndex, np.ndarray]:
    start = pd.Timestamp(daily["sales_date"].min()).normalize()
    end = pd.Timestamp(daily["sales_date"].max()).normalize()
    dates = pd.date_range(start, end, freq="D")
    sku_ids = np.sort(daily["sku_id"].astype("int64").unique())
    sku_lookup = {int(sku): index for index, sku in enumerate(sku_ids)}
    rows = daily["sku_id"].map(sku_lookup).to_numpy(dtype=np.int64)
    columns = (pd.to_datetime(daily["sales_date"]) - start).dt.days.to_numpy(dtype=np.int64)
    values = daily["quantity"].to_numpy(dtype=np.float32)

    matrix = np.zeros((len(sku_ids), len(dates)), dtype=np.float32)
    matrix[rows, columns] = values
    first_indices = np.full(len(sku_ids), len(dates), dtype=np.int64)
    np.minimum.at(first_indices, rows, columns)
    return matrix, sku_ids, dates, first_indices


def _split_counts(max_samples: int) -> tuple[int, int, int]:
    if max_samples < 30:
        raise ValueError("max_samples must be at least 30")
    if max_samples > HARD_SAMPLE_LIMIT:
        raise ValueError(f"max_samples cannot exceed {HARD_SAMPLE_LIMIT}")
    train = int(max_samples * 0.70)
    validation = int(max_samples * 0.15)
    return train, validation, max_samples - train - validation


def _sample_coordinates(
    *,
    matrix: np.ndarray,
    first_indices: np.ndarray,
    column_start: int,
    column_end: int,
    sample_count: int,
    positive_fraction: float,
    rng: np.random.Generator,
) -> tuple[np.ndarray, int, int]:
    width = column_end - column_start
    local_columns = np.arange(column_start, column_end, dtype=np.int64)
    eligible = local_columns[None, :] >= (first_indices[:, None] + 28)
    targets = matrix[:, column_start:column_end]
    positive_pool = np.flatnonzero(eligible & (targets > 0))
    zero_pool = np.flatnonzero(eligible & (targets == 0))
    candidate_count = int(len(positive_pool) + len(zero_pool))
    if candidate_count < sample_count:
        raise ValueError(
            f"split has only {candidate_count} eligible candidates for {sample_count} samples"
        )

    positive_count = min(int(round(sample_count * positive_fraction)), len(positive_pool))
    zero_count = min(sample_count - positive_count, len(zero_pool))
    if positive_count + zero_count < sample_count:
        remaining = sample_count - positive_count - zero_count
        positive_available = len(positive_pool) - positive_count
        if positive_available >= remaining:
            positive_count += remaining
        else:
            zero_count += remaining

    chosen_positive = rng.choice(positive_pool, size=positive_count, replace=False)
    chosen_zero = rng.choice(zero_pool, size=zero_count, replace=False)
    flat = np.concatenate([chosen_positive, chosen_zero])
    rows = flat // width
    columns = flat % width + column_start
    coordinates = np.column_stack([rows, columns])
    order = np.lexsort((coordinates[:, 0], coordinates[:, 1]))
    return coordinates[order], candidate_count, int(len(positive_pool))


def _build_split(
    *,
    name: str,
    coordinates: np.ndarray,
    matrix: np.ndarray,
    sku_ids: np.ndarray,
    dates: pd.DatetimeIndex,
    sku_to_index: dict[int, int],
    category_by_sku: dict[int, str],
    category_to_index: dict[str, int],
    candidate_count: int,
    positive_candidate_count: int,
) -> SampleSplit:
    features: list[list[float]] = []
    targets: list[float] = []
    targets_scaled: list[float] = []
    scales: list[float] = []
    product_indices: list[int] = []
    category_indices: list[int] = []
    target_dates: list[str] = []
    selected_skus: list[int] = []
    lag7_baseline: list[float] = []
    mean28_baseline: list[float] = []

    for row_index, column_index in coordinates:
        sku_id = int(sku_ids[row_index])
        prior = matrix[row_index, column_index - 28 : column_index]
        target_date = dates[column_index].date()
        vector, scale = build_feature_vector(prior.tolist(), target_date)
        target = float(matrix[row_index, column_index])
        category = category_by_sku.get(sku_id, UNKNOWN_CATEGORY)
        features.append(vector)
        targets.append(target)
        targets_scaled.append(target / scale)
        scales.append(scale)
        product_indices.append(sku_to_index.get(sku_id, 0))
        category_indices.append(category_to_index.get(category, 0))
        target_dates.append(target_date.isoformat())
        selected_skus.append(sku_id)
        lag7_baseline.append(float(prior[-7]))
        mean28_baseline.append(float(prior.mean()))

    return SampleSplit(
        name=name,
        features=np.asarray(features, dtype=np.float32),
        targets_scaled=np.asarray(targets_scaled, dtype=np.float32),
        targets=np.asarray(targets, dtype=np.float32),
        scales=np.asarray(scales, dtype=np.float32),
        product_indices=np.asarray(product_indices, dtype=np.int64),
        category_indices=np.asarray(category_indices, dtype=np.int64),
        target_dates=np.asarray(target_dates),
        sku_ids=np.asarray(selected_skus, dtype=np.int64),
        lag7_baseline=np.asarray(lag7_baseline, dtype=np.float32),
        mean28_baseline=np.asarray(mean28_baseline, dtype=np.float32),
        candidate_count=candidate_count,
        positive_candidate_count=positive_candidate_count,
    )


def prepare_daily_samples(
    daily: pd.DataFrame,
    *,
    category_by_sku: dict[int, str] | None = None,
    max_samples: int = HARD_SAMPLE_LIMIT,
    positive_fraction: float = 0.60,
    seed: int = 20260718,
) -> PreparedSamples:
    """Create leakage-safe chronological daily samples capped at 2,000 total."""
    if not 0 < positive_fraction < 1:
        raise ValueError("positive_fraction must be between zero and one")
    train_count, validation_count, test_count = _split_counts(max_samples)
    matrix, sku_ids, dates, first_indices = _dense_daily_matrix(daily)
    eligible_day_count = len(dates) - 28
    if eligible_day_count < 30:
        raise ValueError("daily history must cover at least 58 calendar days")

    first_target_column = 28
    train_day_count = int(eligible_day_count * 0.70)
    validation_day_count = int(eligible_day_count * 0.15)
    train_end = first_target_column + train_day_count
    validation_end = train_end + validation_day_count
    boundaries = {
        "train": (first_target_column, train_end, train_count),
        "validation": (train_end, validation_end, validation_count),
        "test": (validation_end, len(dates), test_count),
    }
    rng = np.random.default_rng(seed)
    sampled: dict[str, tuple[np.ndarray, int, int]] = {}
    for name, (column_start, column_end, count) in boundaries.items():
        sampled[name] = _sample_coordinates(
            matrix=matrix,
            first_indices=first_indices,
            column_start=column_start,
            column_end=column_end,
            sample_count=count,
            positive_fraction=positive_fraction,
            rng=rng,
        )

    train_sku_ids = sorted(
        {int(sku_ids[row]) for row, _ in sampled["train"][0]}
    )
    sku_to_index = {sku_id: index + 1 for index, sku_id in enumerate(train_sku_ids)}
    category_by_sku = category_by_sku or {}
    train_categories = sorted(
        {
            category_by_sku.get(sku_id, UNKNOWN_CATEGORY)
            for sku_id in train_sku_ids
            if category_by_sku.get(sku_id, UNKNOWN_CATEGORY) != UNKNOWN_CATEGORY
        }
    )
    category_to_index = {
        category: index + 1 for index, category in enumerate(train_categories)
    }

    split_objects: dict[str, SampleSplit] = {}
    for name, (coordinates, candidate_count, positive_candidate_count) in sampled.items():
        split_objects[name] = _build_split(
            name=name,
            coordinates=coordinates,
            matrix=matrix,
            sku_ids=sku_ids,
            dates=dates,
            sku_to_index=sku_to_index,
            category_by_sku=category_by_sku,
            category_to_index=category_to_index,
            candidate_count=candidate_count,
            positive_candidate_count=positive_candidate_count,
        )

    prepared = PreparedSamples(
        train=split_objects["train"],
        validation=split_objects["validation"],
        test=split_objects["test"],
        sku_to_index=sku_to_index,
        category_to_index=category_to_index,
        source_start_date=dates[0].date(),
        source_end_date=dates[-1].date(),
    )
    if prepared.total_size > HARD_SAMPLE_LIMIT:
        raise AssertionError("prepared samples exceeded the hard 2,000-row limit")
    if max(prepared.train.target_dates) >= min(prepared.validation.target_dates):
        raise AssertionError("train and validation dates overlap")
    if max(prepared.validation.target_dates) >= min(prepared.test.target_dates):
        raise AssertionError("validation and test dates overlap")
    return prepared


def pinball_loss(predictions: Tensor, targets: Tensor) -> Tensor:
    errors = targets.unsqueeze(1) - predictions
    quantiles = torch.tensor(
        QUANTILES,
        dtype=predictions.dtype,
        device=predictions.device,
    )
    return torch.maximum((quantiles - 1) * errors, quantiles * errors).mean()


def _loader(split: SampleSplit, batch_size: int, shuffle: bool, seed: int) -> DataLoader:
    dataset = TensorDataset(
        torch.from_numpy(split.features),
        torch.from_numpy(split.product_indices),
        torch.from_numpy(split.category_indices),
        torch.from_numpy(split.targets_scaled),
    )
    generator = torch.Generator().manual_seed(seed)
    return DataLoader(
        dataset,
        batch_size=min(batch_size, split.size),
        shuffle=shuffle,
        generator=generator,
    )


def _validation_loss(
    model: GlobalSalesNetwork,
    split: SampleSplit,
    device: torch.device,
    batch_size: int,
) -> float:
    model.eval()
    losses: list[float] = []
    with torch.inference_mode():
        for features, product_ids, category_ids, targets in _loader(
            split, batch_size, False, 0
        ):
            predictions = model(
                features.to(device),
                product_ids.to(device),
                category_ids.to(device),
            )
            losses.append(float(pinball_loss(predictions, targets.to(device)).cpu()))
    return float(np.mean(losses))


def train_network(
    prepared: PreparedSamples,
    *,
    device: torch.device,
    epochs: int = 200,
    patience: int = 25,
    batch_size: int = 128,
    learning_rate: float = 0.003,
    seed: int = 20260718,
) -> tuple[GlobalSalesNetwork, dict[str, Any]]:
    """Train with validation early stopping and entity masking for cold-start SKUs."""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    model = GlobalSalesNetwork(
        num_products=len(prepared.sku_to_index),
        num_categories=len(prepared.category_to_index),
    ).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=learning_rate,
        weight_decay=1e-4,
    )
    train_loader = _loader(prepared.train, batch_size, True, seed)
    best_loss = math.inf
    best_epoch = 0
    best_state: dict[str, Tensor] | None = None
    stale_epochs = 0
    history: list[dict[str, float | int]] = []

    for epoch in range(1, epochs + 1):
        model.train()
        batch_losses: list[float] = []
        for features, product_ids, category_ids, targets in train_loader:
            features = features.to(device)
            product_ids = product_ids.to(device)
            category_ids = category_ids.to(device)
            targets = targets.to(device)
            product_mask = torch.rand(product_ids.shape, device=device) < 0.20
            category_mask = torch.rand(category_ids.shape, device=device) < 0.10
            product_ids = product_ids.masked_fill(product_mask, 0)
            category_ids = category_ids.masked_fill(category_mask, 0)
            optimizer.zero_grad(set_to_none=True)
            predictions = model(features, product_ids, category_ids)
            loss = pinball_loss(predictions, targets)
            if not torch.isfinite(loss):
                raise RuntimeError("training loss became non-finite")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=5.0)
            optimizer.step()
            batch_losses.append(float(loss.detach().cpu()))

        validation_loss = _validation_loss(
            model,
            prepared.validation,
            device,
            batch_size,
        )
        train_loss = float(np.mean(batch_losses))
        history.append(
            {
                "epoch": epoch,
                "train_pinball_scaled": train_loss,
                "validation_pinball_scaled": validation_loss,
            }
        )
        if validation_loss < best_loss - 1e-5:
            best_loss = validation_loss
            best_epoch = epoch
            best_state = {
                key: value.detach().cpu().clone()
                for key, value in model.state_dict().items()
            }
            stale_epochs = 0
        else:
            stale_epochs += 1
            if stale_epochs >= patience:
                break

    if best_state is None:
        raise RuntimeError("training did not produce a valid checkpoint")
    model.load_state_dict(best_state)
    model.to(device)
    model.eval()
    summary = {
        "epochs_requested": epochs,
        "epochs_completed": len(history),
        "best_epoch": best_epoch,
        "best_validation_pinball_scaled": best_loss,
        "early_stopped": len(history) < epochs,
        "history": history,
    }
    return model, summary


def _predict_split(
    model: GlobalSalesNetwork,
    split: SampleSplit,
    device: torch.device,
    interval_expansion_scaled: float = 0.0,
) -> np.ndarray:
    model.eval()
    with torch.inference_mode():
        predictions = model(
            torch.from_numpy(split.features).to(device),
            torch.from_numpy(split.product_indices).to(device),
            torch.from_numpy(split.category_indices).to(device),
        ).cpu().numpy()
    predictions = predictions * split.scales[:, None]
    if interval_expansion_scaled > 0:
        expansion = interval_expansion_scaled * split.scales
        predictions[:, 0] -= expansion
        predictions[:, 2] += expansion
    return np.maximum(0, predictions)


def fit_interval_calibration(
    model: GlobalSalesNetwork,
    validation: SampleSplit,
    device: torch.device,
    nominal_coverage: float = 0.80,
) -> float:
    """Fit a split-conformal symmetric interval expansion on validation only."""
    predictions = _predict_split(model, validation, device)
    lower_error = (predictions[:, 0] - validation.targets) / validation.scales
    upper_error = (validation.targets - predictions[:, 2]) / validation.scales
    scores = np.maximum.reduce(
        [lower_error, upper_error, np.zeros_like(lower_error)]
    )
    finite_sample_level = min(
        1.0,
        math.ceil((validation.size + 1) * nominal_coverage) / validation.size,
    )
    return float(np.quantile(scores, finite_sample_level, method="higher"))


def _point_metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    errors = predicted - actual
    denominator = float(np.abs(actual).sum())
    smape_denominator = np.abs(actual) + np.abs(predicted)
    smape_values = np.divide(
        2 * np.abs(errors),
        smape_denominator,
        out=np.zeros_like(errors),
        where=smape_denominator > 0,
    )
    return {
        "mae": float(np.mean(np.abs(errors))),
        "rmse": float(np.sqrt(np.mean(np.square(errors)))),
        "wape": float(np.abs(errors).sum() / denominator) if denominator else 0.0,
        "smape": float(np.mean(smape_values)),
    }


def evaluate_split(
    model: GlobalSalesNetwork,
    split: SampleSplit,
    device: torch.device,
    interval_expansion_scaled: float = 0.0,
) -> dict[str, Any]:
    predictions = _predict_split(
        model,
        split,
        device,
        interval_expansion_scaled,
    )
    actual = split.targets
    median = predictions[:, 1]
    positive = actual > 0
    metrics: dict[str, Any] = {
        "model": _point_metrics(actual, median),
        "seasonal_naive_lag7": _point_metrics(actual, split.lag7_baseline),
        "rolling_mean_28": _point_metrics(actual, split.mean28_baseline),
        "p10_p90_coverage": float(
            np.mean((actual >= predictions[:, 0]) & (actual <= predictions[:, 2]))
        ),
        "median_bias": float(np.mean(median - actual)),
        "positive_rows": int(positive.sum()),
        "zero_rows": int((~positive).sum()),
    }
    if positive.any():
        metrics["model_positive_targets"] = _point_metrics(
            actual[positive], median[positive]
        )
    return metrics


def _split_summary(split: SampleSplit) -> dict[str, Any]:
    return {
        "samples": split.size,
        "date_start": str(min(split.target_dates)),
        "date_end": str(max(split.target_dates)),
        "unique_skus": int(len(np.unique(split.sku_ids))),
        "positive_targets": int((split.targets > 0).sum()),
        "zero_targets": int((split.targets == 0).sum()),
        "candidate_rows": split.candidate_count,
        "positive_candidate_rows": split.positive_candidate_count,
    }


def run_training(
    *,
    sales_csv: Path,
    catalog_xlsx: Path | None,
    output_path: Path,
    report_path: Path,
    max_samples: int = HARD_SAMPLE_LIMIT,
    positive_fraction: float = 0.60,
    epochs: int = 200,
    patience: int = 25,
    batch_size: int = 128,
    learning_rate: float = 0.003,
    seed: int = 20260718,
    device_name: str = "auto",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Run the complete offline training, evaluation, and artifact workflow."""
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"model artifact already exists: {output_path}")
    if device_name == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is unavailable")
    device = torch.device(
        "cuda" if device_name == "cuda" or (device_name == "auto" and torch.cuda.is_available()) else "cpu"
    )
    daily, audit = load_daily_sales(sales_csv)
    category_by_sku = load_category_map(catalog_xlsx)
    prepared = prepare_daily_samples(
        daily,
        category_by_sku=category_by_sku,
        max_samples=max_samples,
        positive_fraction=positive_fraction,
        seed=seed,
    )
    model, training_summary = train_network(
        prepared,
        device=device,
        epochs=epochs,
        patience=patience,
        batch_size=batch_size,
        learning_rate=learning_rate,
        seed=seed,
    )
    nominal_interval_coverage = 0.80
    interval_expansion_scaled = fit_interval_calibration(
        model,
        prepared.validation,
        device,
        nominal_interval_coverage,
    )
    validation_metrics = evaluate_split(
        model,
        prepared.validation,
        device,
        interval_expansion_scaled,
    )
    test_metrics = evaluate_split(
        model,
        prepared.test,
        device,
        interval_expansion_scaled,
    )

    architecture = {
        "num_products": len(prepared.sku_to_index),
        "num_categories": len(prepared.category_to_index),
        "product_embedding_dim": 8,
        "category_embedding_dim": 4,
        "hidden_sizes": [128, 64],
        "dropout": 0.1,
    }
    trained_through = str(max(prepared.train.target_dates))
    model_version = f"torch-global-daily-{trained_through}-s{seed}"
    artifact = {
        "format_version": MODEL_FORMAT_VERSION,
        "model_state_dict": {
            key: value.detach().cpu() for key, value in model.state_dict().items()
        },
        "model_version": model_version,
        "trained_through": trained_through,
        "product_id_to_index": {},
        "sku_to_index": {
            str(key): value for key, value in prepared.sku_to_index.items()
        },
        "category_to_index": prepared.category_to_index,
        "architecture": architecture,
        "feature_names": list(FEATURE_NAMES),
        "quantiles": list(QUANTILES),
        "interval_calibration_scaled": interval_expansion_scaled,
        "training_metadata": {
            "source_grain": "sku_day",
            "source_date_start": audit.start_date.isoformat(),
            "source_date_end": audit.end_date.isoformat(),
            "sample_limit": HARD_SAMPLE_LIMIT,
            "samples_used": prepared.total_size,
            "validation_through": str(max(prepared.validation.target_dates)),
            "test_through": str(max(prepared.test.target_dates)),
            "seed": seed,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_suffix(output_path.suffix + ".tmp")
    torch.save(artifact, temporary_output)
    temporary_output.replace(output_path)

    report = {
        "status": "completed",
        "model_version": model_version,
        "model_artifact": str(output_path),
        "device": str(device),
        "source_files": {
            "sales_transactions": str(sales_csv),
            "category_catalog": str(catalog_xlsx) if catalog_xlsx else None,
        },
        "data_contract": {
            "source_grain": "SKU daily sales aggregated from transaction rows",
            "promotion_window_rows_used": False,
            "overlapping_promotions_apportioned": False,
            "history_days_per_sample": 28,
            "target": "next-day completed positive sales quantity; missing SKU-days are zero after first observation",
        },
        "source_audit": {
            "source_rows": audit.source_rows,
            "valid_completed_positive_rows": audit.valid_rows,
            "daily_sku_rows": audit.daily_rows,
            "unique_skus": audit.unique_skus,
            "date_start": audit.start_date.isoformat(),
            "date_end": audit.end_date.isoformat(),
            "rejected_unfinished_rows": audit.rejected_unfinished_rows,
            "rejected_non_positive_rows": audit.rejected_non_positive_rows,
            "rejected_invalid_rows": audit.rejected_invalid_rows,
        },
        "sample_policy": {
            "hard_total_limit": HARD_SAMPLE_LIMIT,
            "samples_used": prepared.total_size,
            "positive_sampling_fraction": positive_fraction,
            "split_method": "strict chronological target-date ranges, then deterministic stratified sampling",
            "seed": seed,
        },
        "splits": {
            "train": _split_summary(prepared.train),
            "validation": _split_summary(prepared.validation),
            "test": _split_summary(prepared.test),
        },
        "training": training_summary,
        "interval_calibration": {
            "method": "split_conformal_symmetric_scaled",
            "fit_split": "validation",
            "nominal_coverage": nominal_interval_coverage,
            "expansion_scaled": interval_expansion_scaled,
        },
        "metrics": {
            "validation": validation_metrics,
            "test": test_metrics,
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_report = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary_report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_report.replace(report_path)
    return report
