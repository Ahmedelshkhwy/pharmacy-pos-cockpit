"""Transfers, purchase orders, ABC/XYZ: rules that must always hold."""
from __future__ import annotations

import csv
import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "sample_data"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SAMPLES))

import generate_samples  # noqa: E402
from actions import plan_transfers, purchase_orders, shortage, surplus, target_stock  # noqa: E402
from forecast import weekly_variability  # noqa: E402
from settings import AnalysisSettings  # noqa: E402
from smart_import import detect_file  # noqa: E402
from smart_pipeline import abc_classes, run_smart_import  # noqa: E402

SETTINGS = AnalysisSettings()


def item(branch, sku, stock, rate, **extra):
    row = {
        "branch": branch, "sku": sku, "barcode": extra.pop("barcode", ""), "name": extra.pop("name", sku), "supplier": extra.pop("supplier", "S1"),
        "stock": stock, "avg_daily_sales": rate, "reorder_point": rate * 7 + 1.65 * math.sqrt(rate * 7) if rate else 0,
        "cost_price": 10.0, "price": 13.0, "tags": "", "expired_qty": 0, "expiry_risk_qty": 0, "days_left": None,
        "stockout_probability_7d": 0.0, "stockout_probability_14d": 0.0, "frozen_capital_value": 0, "negative_margin_loss": 0,
    }
    row.update(extra)
    row["recommended_reorder_qty"] = math.ceil(shortage(row, SETTINGS))
    return row


def test_dead_stock_moves_to_the_branch_that_ran_out():
    rows = [
        item("North", "P1", 30, 0.0, tags="dead_stock"),
        item("South", "P1", 0, 1.0, stockout_probability_7d=1.0, stockout_probability_14d=1.0, tags="stockout_with_sales"),
    ]
    [transfer] = plan_transfers(rows, SETTINGS)
    assert (transfer.from_branch, transfer.to_branch, transfer.reason) == ("North", "South", "dead")
    assert transfer.qty == min(30, math.floor(shortage(rows[1], SETTINGS)))
    assert transfer.receiver_urgent


def test_donor_keeps_enough_for_itself():
    rows = [item("North", "P1", 100, 1.0), item("South", "P1", 0, 2.0)]
    [transfer] = plan_transfers(rows, SETTINGS)
    assert rows[0]["stock"] - transfer.qty >= target_stock(rows[0], SETTINGS) - 1


def test_expired_units_never_move_and_expiring_units_only_if_they_can_sell():
    donor = item("North", "P1", 50, 0.1, expired_qty=10, expiry_risk_qty=50, days_left=20)
    receiver = item("South", "P1", 0, 1.0)
    [transfer] = plan_transfers([donor, receiver], SETTINGS)
    assert transfer.reason == "expiry"
    assert transfer.qty <= 20  # South sells 1/day: only 20 units can go before expiry
    assert transfer.qty <= 50 - 10  # the 10 expired units stay


def test_items_match_across_code_systems():
    rows = [
        item("North", "A1000", 40, 0.0, name="Panadol Extra 24 Tab", tags="dead_stock"),
        item("South", "6221234567890", 0, 1.0, barcode="6221234567890", name="PANADOL EXTRA 24 TAB"),
    ]
    assert len(plan_transfers(rows, SETTINGS)) == 1


def test_purchase_orders_subtract_transfers_and_skip_below_cost_items():
    rows = [
        item("North", "P1", 10, 0.0, tags="dead_stock"),
        item("South", "P1", 0, 1.0),
        item("South", "P2", 0, 1.0, tags="negative_margin", negative_margin_loss=50),
    ]
    transfers = plan_transfers(rows, SETTINGS)
    [order] = purchase_orders(rows, transfers)
    [line] = order.lines
    assert line.sku == "P1" and line.covered_by_transfer == 10
    assert line.qty == math.ceil(rows[1]["recommended_reorder_qty"] - 10)


def test_transfer_invariants_on_chain_sample(tmp_path):
    generate_samples.main()
    path = SAMPLES / "D_chain_multi_branch.xlsx"
    result = run_smart_import([(path, spec) for spec in detect_file(path)], tmp_path, today=date(2026, 10, 31))
    rows = []
    for branch in result.branches:
        for row in csv.DictReader((branch.output_dir / "merged_items.csv").open(encoding="utf-8-sig")):
            rows.append({
                "branch": row["branch"], "sku": row["ref"], "barcode": row["barcode"], "name": row["name"], "supplier": row["supplier"],
                "stock": float(row["qoh"]), "avg_daily_sales": float(row["avg_daily_sales"]), "reorder_point": float(row["reorder_point"]),
                "recommended_reorder_qty": float(row["recommended_reorder_qty"]), "cost_price": float(row["cost"]), "price": float(row["sales_price"]),
                "tags": row["tags"], "expired_qty": float(row["expired_qty"]), "expiry_risk_qty": float(row["expiry_risk_qty"]),
                "days_left": int(row["days_left"]) if row["days_left"] else None,
                "stockout_probability_7d": float(row["stockout_probability_7d"]), "stockout_probability_14d": float(row["stockout_probability_14d"]),
            })
    transfers = plan_transfers(rows, SETTINGS)
    assert transfers
    by_key = {(row["branch"], row["sku"]): row for row in rows}
    sent, received = {}, {}
    for transfer in transfers:
        sent[(transfer.from_branch, transfer.from_sku)] = sent.get((transfer.from_branch, transfer.from_sku), 0) + transfer.qty
        received[(transfer.to_branch, transfer.sku)] = received.get((transfer.to_branch, transfer.sku), 0) + transfer.qty
    for key, qty in sent.items():
        assert qty <= math.floor(surplus(by_key[key], SETTINGS))
    for key, qty in received.items():
        assert qty <= math.floor(shortage(by_key[key], SETTINGS))


def test_abc_classes_follow_cumulative_share():
    items = [{"pos_sales": 1, "gross_profit": value} for value in (50, 30, 10, 6, 4)] + [{"pos_sales": 0, "gross_profit": 0}]
    assert abc_classes(items, "gross_profit") == ["A", "A", "B", "B", "C", ""]


def test_xyz_from_weekly_variability():
    steady = {day: 2.0 for day in range(84)}
    lumpy = {day: 40.0 for day in range(0, 84, 21)}
    assert weekly_variability(steady, 84) == 0
    assert weekly_variability(lumpy, 84) > 1.0
    assert weekly_variability(steady, 20) is None  # under 4 weeks


def test_a_items_get_more_safety_stock(tmp_path):
    path = SAMPLES / "A_arabic_pharmacy_report.xlsx"
    tables = [(path, spec) for spec in detect_file(path)]

    def reorder_points(settings):
        result = run_smart_import(tables, tmp_path, today=date(2026, 10, 31), settings=settings)
        return {row["ref"]: (row["abc_class"], float(row["reorder_point"])) for row in csv.DictReader((result.branches[0].output_dir / "merged_items.csv").open(encoding="utf-8-sig"))}

    flat = reorder_points(AnalysisSettings(service_level_a=95, service_level_b=95, service_level_c=95))
    tiered = reorder_points(AnalysisSettings())
    for ref, (abc, point) in tiered.items():
        if flat[ref][1] > 0:
            if abc == "A":
                assert point > flat[ref][1]
            elif abc == "C":
                assert point < flat[ref][1]
