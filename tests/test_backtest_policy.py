"""The purchasing backtest: bookkeeping and budget limits on a small synthetic store."""
from __future__ import annotations

import random
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import backtest_policy as bt  # noqa: E402
from settings import AnalysisSettings  # noqa: E402

START = date(2026, 1, 1)


def store(days=200, seed=3):
    rng = random.Random(seed)
    items = []
    for index, (rate, price, margin) in enumerate([(6, 2, 0.1), (3, 10, 0.3), (1, 40, 0.35), (0.3, 120, 0.4), (2, 5, 0.2)]):
        demand = {}
        for day in range(days):
            qty = sum(1 for _ in range(int(rate * 3)) if rng.random() < 1 / 3)
            if qty:
                demand[day] = qty
        items.append(bt.Item(ref=f"1{index}", name=f"item {index}", price=price, cost=price * (1 - margin), demand=demand))
    return items


def test_unlimited_serves_most_demand_and_books_balance():
    items = store()
    totals = bt.simulate(items, START, 90, 200, AnalysisSettings(), "unlimited")
    assert totals.days == 110 and totals.reviews == 16
    assert totals.sold_units + totals.lost_units == pytest.approx(totals.demand_units)
    assert totals.summary()["fill_rate"] > 90


@pytest.mark.parametrize("policy", ["priority", "optimized"])
def test_budget_is_never_exceeded(policy):
    items = store()
    budget = 60.0
    totals = bt.simulate(items, START, 90, 200, AnalysisSettings(), policy, weekly_budget=budget)
    assert totals.review_spend and max(totals.review_spend) <= budget + 1e-6
    assert totals.sold_units + totals.lost_units == pytest.approx(totals.demand_units)
    unlimited = bt.simulate(items, START, 90, 200, AnalysisSettings(), "unlimited")
    assert totals.summary()["fill_rate"] <= unlimited.summary()["fill_rate"] + 1e-9


def test_report_mentions_both_policies():
    results = {
        "assumptions": {"file": "x.xlsx", "items": 5, "from": "2026-04-01", "to": "2026-07-19", "days": 110,
                        "lead_time_days": 7, "target_cover_days": 30, "customer_loss_percent": 20, "margin": [0.15, 0.4]},
        "unlimited": {"weekly_spend": 100.0, "fill_rate": 97.0},
        "runs": [{"ratio": 0.5, "weekly_budget": 50.0,
                  "priority": {"spent": 700, "value_fill_rate": 70, "fill_rate": 75, "lost_profit": 300, "lost_sales": 900, "stockout_days": 40, "avg_inventory": 200, "gmroi": 2.0},
                  "optimized": {"spent": 720, "value_fill_rate": 74, "fill_rate": 73, "lost_profit": 240, "lost_sales": 850, "stockout_days": 30, "avg_inventory": 210, "gmroi": 2.2}}],
        "seconds": 1,
    }
    text = bt.report(results)
    assert "Priority order" in text and "Budget plan" in text and "20.0% less" in text


def test_items_are_picked_without_looking_ahead():
    raw = {
        "100": {"name": "early", "price": 10.0, "daily": {"5": 50}},
        "200": {"name": "late", "price": 10.0, "daily": {"95": 500}},
        "POST": {"name": "postage", "price": 10.0, "daily": {"5": 999}},
    }
    picked = bt.pick_items(raw, sim_start=90, count=5, margin=(0.2, 0.2))
    assert [item.ref for item in picked] == ["100"]
    assert picked[0].cost == pytest.approx(8.0)
