"""Backtest the demand forecast on any invoice-level file.

For each cutoff date the forecast only sees invoices before the cutoff (a 90-day upload,
or a 365-day upload), predicts the next 30 days per item, and is scored on what really
sold. WAPE = sum |forecast - actual| / sum actual (lower is better); bias = forecast / actual - 1.

Run:  python tools/backtest_forecast.py sample_data/uci/online_retail_II.xlsx
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forecast import DayWeights, forecast_item, to_day_index  # noqa: E402
from smart_import import Quality, detect_file, iter_records  # noqa: E402

HORIZON = 30


def load_daily(paths: list[Path]) -> dict[str, dict[date, float]]:
    daily: dict[str, dict[date, float]] = defaultdict(dict)
    seen: set[int] = set()
    for path in paths:
        for spec in detect_file(path):
            if "transactions" not in spec.role:
                continue
            for record in iter_records(path, spec, Quality()):
                key = hash((record.get("invoice"), record.get("ref"), record.get("qty_sold"), record.get("price"), record.get("date")))
                if record.get("invoice") and key in seen:
                    continue
                seen.add(key)
                day, qty = record.get("date"), record.get("qty_sold")
                ref = record.get("ref") or record.get("barcode") or record.get("name")
                if day and qty is not None and ref:
                    daily[ref][day] = daily[ref].get(day, 0.0) + qty
    return daily


def evaluate(daily: dict[str, dict[date, float]], cutoff: date, window: int, half_life: float, correct: bool) -> tuple[float, float, int]:
    start = cutoff - timedelta(days=window)
    history = {ref: {d: q for d, q in days.items() if start <= d < cutoff} for ref, days in daily.items()}
    weights = DayWeights(start, window, half_life)
    abs_error = forecast_total = actual_total = 0.0
    count = 0
    for ref, days in daily.items():
        past = history[ref]
        actual = max(sum(q for d, q in days.items() if cutoff <= d < cutoff + timedelta(days=HORIZON)), 0.0)
        if not past and not actual:
            continue
        predicted = forecast_item(to_day_index(past, start), weights, qoh=None, correct_stockouts=correct).rate * HORIZON if past else 0.0
        abs_error += abs(predicted - actual)
        forecast_total += predicted
        actual_total += actual
        count += 1
    return abs_error / actual_total, forecast_total / actual_total - 1, count


METHODS = [
    # label, window, half-life (0 = no recency weighting), unavailable-days correction
    ("90 days: plain average (old)", 90, 0, False),
    ("90 days: + unavailable-days correction", 90, 0, True),
    ("90 days: + correction + recency half-life 15d", 90, 15, True),
    ("90 days: + correction + recency half-life 21d", 90, 21, True),
    ("90 days: + correction + recency half-life 30d", 90, 30, True),
    ("90 days: + correction + recency half-life 45d", 90, 45, True),
    ("365 days: plain average (old)", 365, 0, False),
    ("365 days: + correction + recency half-life 30d", 365, 30, True),
    ("365 days: + correction + recency half-life 60d", 365, 60, True),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--cutoffs", nargs="*", default=["2011-03-01", "2011-05-01", "2011-07-01", "2011-09-01", "2011-11-01"])
    args = parser.parse_args()
    started = time.time()
    daily = load_daily(args.files)
    print(f"Loaded {len(daily):,} items in {time.time() - started:.0f}s")
    cutoffs = [date.fromisoformat(value) for value in args.cutoffs]
    print(f"{'method':52} " + " ".join(f"{c.isoformat():>11}" for c in cutoffs) + "   mean WAPE   mean bias")
    for label, window, half_life, correct in METHODS:
        scores = [evaluate(daily, cutoff, window, half_life, correct) for cutoff in cutoffs]
        wapes = [score[0] for score in scores]
        biases = [score[1] for score in scores]
        print(f"{label:52} " + " ".join(f"{w:11.3f}" for w in wapes) + f"   {sum(wapes) / len(wapes):9.3f}   {sum(biases) / len(biases):+9.1%}")


if __name__ == "__main__":
    main()
