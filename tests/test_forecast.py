"""Demand forecast: stockout correction, lumpy demand, and settings that drive it."""
from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "sample_data"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SAMPLES))

import generate_samples  # noqa: E402
from forecast import DayWeights, forecast_item  # noqa: E402
from settings import AnalysisSettings  # noqa: E402
from smart_import import detect_file  # noqa: E402
from smart_pipeline import run_smart_import  # noqa: E402

START = date(2026, 8, 1)


def test_ran_out_items_are_not_mistaken_for_low_demand():
    daily = {day: 2.0 for day in range(60)}  # sold 2/day, then empty shelf for 30 days
    weights = DayWeights(START, 90, half_life=0)
    assert forecast_item(daily, weights, qoh=0).rate == 2.0
    # Same sales, but stock was on the shelf the whole time: demand really dropped.
    assert forecast_item(daily, weights, qoh=25).rate == 120 / 90


def test_mid_period_stockout_gap_is_removed():
    daily = {day: 3.0 for day in range(90) if not 30 <= day < 50}
    result = forecast_item(daily, DayWeights(START, 90, half_life=0), qoh=40)
    assert result.unavailable_days == 20 and result.rate == 3.0


def test_lumpy_orders_are_not_treated_as_stockouts():
    daily = {day: 50.0 for day in range(0, 90, 9)}  # a big order every 9 days is normal for this item
    result = forecast_item(daily, DayWeights(START, 90, half_life=0), qoh=None)
    assert result.unavailable_days == 0


def test_recent_days_weigh_more():
    daily = {day: (1.0 if day < 60 else 4.0) for day in range(90)}
    flat = forecast_item(daily, DayWeights(START, 90, half_life=0), qoh=10).rate
    recent = forecast_item(daily, DayWeights(START, 90, half_life=30), qoh=10).rate
    assert flat == 2.0 and recent > 2.5
    assert forecast_item(daily, DayWeights(START, 90, half_life=30), qoh=10).trend == "up"


def test_stockout_correction_recovers_true_demand_on_invoice_sample(tmp_path):
    generate_samples.main()
    tables = [(path, spec) for path in [SAMPLES / "B_sales_invoices.csv", SAMPLES / "B_stock_batches.csv"] for spec in detect_file(path)]
    truth = {item.barcode: item for item in generate_samples.build_branch(23, "B")}

    def mean_error(settings: AnalysisSettings) -> float:
        result = run_smart_import(tables, tmp_path, default_branch="x", today=date(2026, 10, 31), settings=settings)
        rows = csv.DictReader((result.branches[0].output_dir / "merged_items.csv").open(encoding="utf-8-sig"))
        errors = [
            abs(float(row["avg_daily_sales"]) - truth[row["barcode"]].daily_rate) / truth[row["barcode"]].daily_rate
            for row in rows
            if truth[row["barcode"]].unavailable_days and truth[row["barcode"]].daily_rate > 0
        ]
        assert errors
        return sum(errors) / len(errors)

    plain = mean_error(AnalysisSettings(correct_stockouts=False, recency_half_life_days=0))
    corrected = mean_error(AnalysisSettings(recency_half_life_days=0))
    assert corrected < plain / 3


def test_settings_validation_and_lead_time_precedence():
    settings = AnalysisSettings.from_dict(
        {
            "lead_time_days": "999",
            "service_level_a": "abc",
            "service_level_percent": 97,  # saved before per-class levels existed
            "correct_stockouts": "0",
            "lead_time_overrides": [
                {"kind": "category", "name": "Antibiotics", "days": 3},
                {"kind": "supplier", "name": "Pfizer", "days": 10},
                {"kind": "bogus", "name": "x", "days": 1},
                {"kind": "supplier", "name": "", "days": 5},
            ],
        }
    )
    assert settings.lead_time_days == 120  # clamped to the maximum
    assert settings.service_level_a == 98.0  # invalid -> default
    assert settings.service_level_b == 97.0  # old single level carries over to B
    assert settings.correct_stockouts is False
    assert len(settings.lead_time_overrides) == 2
    assert settings.lead_time_for("pfizer", "Antibiotics") == 10  # supplier beats category, case-insensitive
    assert settings.lead_time_for("Other", "antibiotics") == 3
    assert settings.lead_time_for("Other", "Vitamins") == 120
