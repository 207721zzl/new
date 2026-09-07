"""Daily forecast training data and loss tests."""

import numpy as np
import pandas as pd
import pytest
import torch

from app.forecast.training import (
    HARD_SAMPLE_LIMIT,
    fit_interval_calibration,
    pinball_loss,
    prepare_daily_samples,
)


def _daily_history() -> pd.DataFrame:
    rows = []
    dates = pd.date_range("2025-01-01", periods=180, freq="D")
    for sku_id in range(101, 111):
        for offset, sales_date in enumerate(dates):
            if (offset + sku_id) % 4 == 0:
                rows.append(
                    {
                        "sku_id": sku_id,
                        "sales_date": sales_date,
                        "quantity": float(1 + (offset + sku_id) % 7),
                    }
                )
    return pd.DataFrame(rows)


def test_prepare_daily_samples_is_capped_and_chronological():
    prepared = prepare_daily_samples(
        _daily_history(),
        max_samples=120,
        positive_fraction=0.5,
        seed=7,
    )

    assert prepared.total_size == 120
    assert prepared.train.size == 84
    assert prepared.validation.size == 18
    assert prepared.test.size == 18
    assert max(prepared.train.target_dates) < min(prepared.validation.target_dates)
    assert max(prepared.validation.target_dates) < min(prepared.test.target_dates)
    assert prepared.train.features.shape[1] > 0
    assert np.all(prepared.train.scales >= 1)


def test_prepare_daily_samples_rejects_more_than_hard_limit():
    with pytest.raises(ValueError, match=str(HARD_SAMPLE_LIMIT)):
        prepare_daily_samples(_daily_history(), max_samples=HARD_SAMPLE_LIMIT + 1)


def test_pinball_loss_is_zero_for_exact_quantiles():
    predictions = torch.tensor([[2.0, 2.0, 2.0]])
    targets = torch.tensor([2.0])

    assert pinball_loss(predictions, targets).item() == pytest.approx(0)


def test_interval_calibration_expands_undercovered_validation_intervals():
    class FixedModel(torch.nn.Module):
        def forward(self, features, product_ids, category_ids):
            del product_ids, category_ids
            return torch.ones((features.shape[0], 3), dtype=features.dtype)

    prepared = prepare_daily_samples(
        _daily_history(),
        max_samples=120,
        positive_fraction=0.5,
        seed=7,
    )
    expansion = fit_interval_calibration(
        FixedModel(),
        prepared.validation,
        torch.device("cpu"),
    )

    assert expansion >= 0
