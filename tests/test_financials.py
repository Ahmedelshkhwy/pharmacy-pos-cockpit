"""The money math, checked against numbers worked out by hand."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from insights import analyze_profit_loss  # noqa: E402
from pos_pipeline import poisson_stockout_probability, predictive_metrics  # noqa: E402

COVERAGE = {"sales": True, "stock": True, "cost": True, "price": True, "expiry": True}


def test_reorder_point_and_quantity():
    # 2 units/day, 7-day lead time, 30-day cover, z = 1.65:
    # lead demand 14, safety 1.65 * sqrt(14) = 6.17, reorder point 20.17, target 80.17, stock 10 -> order 71.
    m = predictive_metrics(pos_sales=180, qoh=10, sales_price=50, cost=40, stock_value=500, cost_value=400, turnover_ratio=18, period_days=90)
    assert m["avg_daily_sales"] == 2
    assert m["reorder_point"] == pytest.approx(14 + 1.65 * math.sqrt(14), abs=0.01)
    assert m["recommended_reorder_qty"] == math.ceil(14 + 1.65 * math.sqrt(14) + 60 - 10)
    assert m["lost_sales_value"] == (2 * 30 - 10) * 50  # 50 units short over 30 days at the sale price
    assert m["unit_profit"] == 10 and m["gross_margin_percent"] == 20


def test_stockout_probability():
    assert poisson_stockout_probability(0, 5, 7) == 0  # no demand, no stockout
    assert poisson_stockout_probability(1, 0, 7) == 1  # demand and nothing on the shelf
    # 1/day for 7 days with 7 in stock: P(demand > 7) for Poisson(7) = 1 - CDF(7) ~= 0.401
    assert poisson_stockout_probability(1, 7, 7) == pytest.approx(0.4013, abs=0.001)


def test_frozen_capital_for_dead_stock_is_its_cost():
    m = predictive_metrics(pos_sales=0, qoh=30, sales_price=50, cost=40, stock_value=1500, cost_value=1200, turnover_ratio=0, period_days=90)
    assert m["frozen_capital_value"] == 1200
    assert m["decision_label"] == "Clear dead stock"


def item(**values):
    base = {"name": "x", "ref": "x", "branch": "b", "category": "c", "pos_sales": 0, "net_sales": 0, "gross_profit": 0, "cost": 0, "cost_known": 1,
            "qoh": 0, "negative_margin_loss": 0, "expiry_risk_value": 0, "lost_profit_value": 0, "lost_sales_value": 0, "cost_value": 0,
            "excess_value": 0, "tags": "normal", "gross_margin_percent": 0}
    base.update(values)
    return base


def test_profit_and_losses_add_up_without_double_counting():
    items = [
        item(name="seller", pos_sales=100, net_sales=5000, cost=40, gross_profit=1000, gross_margin_percent=20),
        item(name="below cost", pos_sales=10, net_sales=360, cost=40, gross_profit=-40, negative_margin_loss=40, tags="negative_margin"),
        item(name="expiring", qoh=20, cost=30, expiry_risk_value=600),
        item(name="short", pos_sales=50, net_sales=2500, cost=40, gross_profit=500, lost_profit_value=300, tags="stockout_with_sales"),
        item(name="dead", qoh=25, cost=20, cost_value=500, tags="dead_stock"),
        item(name="excess", pos_sales=5, net_sales=250, cost=40, gross_profit=50, excess_value=800, tags="overstock_risk"),
    ]
    pl = analyze_profit_loss(items, period_days=90, coverage=COVERAGE)
    assert pl["revenue"] == 5000 + 360 + 2500 + 250
    assert pl["gross_profit"] == 1000 - 40 + 500 + 50
    assert pl["margin_pct"] == pytest.approx((1000 - 40 + 500 + 50) / (5000 + 360 + 2500 + 250) * 100, abs=0.01)
    drivers = {driver["key"]: driver["amount"] for driver in pl["drivers"]}
    assert drivers["negative_margin"] == 40 and drivers["expiry"] == 600 and drivers["stockout"] == 300
    assert drivers["dead_stock"] == 500 and drivers["overstock"] == 800
    assert pl["total_loss"] == 40 + 600 + 300  # losses
    assert pl["total_frozen"] == 500 + 800  # frozen cash is reported apart, never added to losses


def test_without_cost_profit_is_not_guessed():
    items = [item(pos_sales=10, net_sales=500, cost=0, cost_known=0, lost_sales_value=200, qoh=0, tags="stockout_with_sales")]
    pl = analyze_profit_loss(items, period_days=30, coverage={**COVERAGE, "cost": False})
    assert pl["gross_profit"] == 0 and "no_cost" in pl["warnings"]
    assert {driver["key"]: driver["amount"] for driver in pl["drivers"]}["stockout"] == 200  # lost sales, labelled as sales
