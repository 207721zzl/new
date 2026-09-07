"""Train the global daily PyTorch sales forecaster from transaction data."""

from argparse import ArgumentParser
import json
from pathlib import Path

from app.forecast.training import HARD_SAMPLE_LIMIT, run_training


def _bounded_sample_count(value: str) -> int:
    parsed = int(value)
    if parsed < 30 or parsed > HARD_SAMPLE_LIMIT:
        raise ValueError(f"sample count must be between 30 and {HARD_SAMPLE_LIMIT}")
    return parsed


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--sales-csv", type=Path, required=True)
    parser.add_argument("--catalog-xlsx", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("models/forecast/global_torch_forecaster.pt"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/forecast_training_report.json"),
    )
    parser.add_argument("--max-samples", type=_bounded_sample_count, default=HARD_SAMPLE_LIMIT)
    parser.add_argument("--positive-fraction", type=float, default=0.60)
    parser.add_argument("--epochs", type=int, default=200)
    parser.add_argument("--patience", type=int, default=25)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--learning-rate", type=float, default=0.003)
    parser.add_argument("--seed", type=int, default=20260718)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report = run_training(
        sales_csv=args.sales_csv,
        catalog_xlsx=args.catalog_xlsx,
        output_path=args.output,
        report_path=args.report,
        max_samples=args.max_samples,
        positive_fraction=args.positive_fraction,
        epochs=args.epochs,
        patience=args.patience,
        batch_size=args.batch_size,
        learning_rate=args.learning_rate,
        seed=args.seed,
        device_name=args.device,
        overwrite=args.overwrite,
    )
    print(json.dumps({
        "status": report["status"],
        "model_version": report["model_version"],
        "model_artifact": report["model_artifact"],
        "samples_used": report["sample_policy"]["samples_used"],
        "test_metrics": report["metrics"]["test"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
