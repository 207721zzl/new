# Global PyTorch daily sales forecast contract

> **Legacy notice:** This document describes the retired InsightAgent sales-forecast module. EvidenceRAG does not load, expose, or route to this module in its current API or Docker deployment. It is retained only as rollback and historical design reference; see `../LEGACY.md`.

The forecast API loads a pre-trained artifact and never trains inside a request. Offline training is implemented by `python -m scripts.train_forecast`.

## Training source and grain

Training consumes transaction rows with these minimum columns:

- `create_dt`: transaction calendar date;
- `sku_id`: stable SKU identifier;
- `is_finished`: only value `1` is retained;
- `sku_cnt`: only positive quantities are retained.

Valid rows are aggregated to one row per SKU and calendar day. After a SKU's first observation, missing calendar days are real zero-sales days. Each target uses exactly the preceding 28 daily observations. Promotion-window rows and promotion totals are not training labels, so overlapping promotions are neither split nor apportioned.

An optional `sku_category` worksheet may supply `sku_id` and `third_category_name`.

## Sample and split policy

- Train, validation, and test contain at most 2,000 samples in total; the CLI rejects a larger value.
- The default allocation is 1,400 / 300 / 300.
- Target-date ranges are strictly chronological and non-overlapping.
- Sampling is deterministic and stratified between positive and zero targets.
- All time-series features use dates strictly before the target date.
- Validation selects the early-stopping checkpoint and fits interval calibration; test data is used only for final metrics.

## Network and features

- One `GlobalSalesNetwork` shares parameters across SKUs.
- SKU and category embeddings are supported; entity masking trains index zero as a cold-start path.
- Lags: 1, 2, 3, 6, 7, 14, 21, and 28 days.
- Rolling means and standard deviations: 7, 14, and 28 days.
- Calendar features: weekday, annual cycle, and weekend flag.
- Scale feature: logarithm of the trailing 28-day mean.
- Outputs: P10, P50, and P90 with structural ordering.

The loss is mean pinball loss on targets normalized by the trailing scale. P10/P90 are expanded by a symmetric, scale-normalized split-conformal correction fit only on validation data.

## Offline command

```powershell
python -m scripts.train_forecast `
  --sales-csv data/forecast/sales_transactions.csv `
  --catalog-xlsx data/forecast/catalog.xlsx `
  --max-samples 2000
```

The default outputs are:

- `models/forecast/global_torch_forecaster.pt`;
- `reports/forecast_training_report.json`.

Use `--overwrite` only when intentionally replacing an existing artifact.

## Inference inputs

Daily history contains `sales_date`, `product_id`, and `quantity`. The catalog contains `product_id`, `sku`, `name`, `category`, and `stock_quantity`. Inference first maps the catalog `sku` through `sku_to_index`, falls back to legacy `product_id_to_index`, and finally uses trained cold-start index zero.

## Artifact fields

The default artifact path is `models/forecast/global_torch_forecaster.pt`. It contains:

- `format_version`, `model_version`, and `trained_through`;
- `model_state_dict` and `architecture`;
- `product_id_to_index`, `sku_to_index`, and `category_to_index`;
- `feature_names` and quantiles `[0.1, 0.5, 0.9]`;
- `interval_calibration_scaled` and `training_metadata`.

Legacy artifacts without `sku_to_index` or interval calibration remain loadable. Missing, incompatible, or unsupported artifacts produce `forecast_unavailable`; the API does not train or download a replacement.
