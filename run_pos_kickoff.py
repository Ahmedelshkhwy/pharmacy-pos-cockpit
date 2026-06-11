from __future__ import annotations

import argparse
from pathlib import Path

from pos_pipeline import analyze_pos_exports


BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Run kick-off analysis for exported POS Excel files.")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=BASE_DIR.parent,
        help="Folder containing POS exports such as i-7-PRODUCT ACTIVITY.xlsx and i-7-STOCK.xlsx.",
    )
    parser.add_argument("--prefix", default="i-7-", help="POS export filename prefix.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=BASE_DIR / "instance" / "pos_analysis",
        help="Folder where analysis outputs are written.",
    )
    parser.add_argument(
        "--period-days",
        type=int,
        default=None,
        help="Sales movement window to use for calculations. Use any positive number of days that matches Product Activity export dates.",
    )
    args = parser.parse_args()

    if args.period_days is not None and args.period_days <= 0:
        raise SystemExit("--period-days must be a positive integer.")

    output_dir = analyze_pos_exports(
        args.source_dir,
        args.output_dir,
        prefix=args.prefix,
        movement_period_days_override=args.period_days,
    )
    print(f"POS kick-off analysis written to: {output_dir}")


if __name__ == "__main__":
    main()
