"""Budget purchase plan: the solver's choice checked against hand-worked answers and brute force."""
from __future__ import annotations

import itertools
import math
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import budget_plan  # noqa: E402
from actions import OrderLine, PurchaseOrder  # noqa: E402
from settings import AnalysisSettings  # noqa: E402

SETTINGS = AnalysisSettings(lead_time_days=5, target_cover_days=5, customer_loss_percent=0)


def line(sku, qty, cost, daily, stock=0, priority="urgent"):
    return OrderLine(sku=sku, barcode="", name=sku, stock=stock, daily=daily, qty=qty, unit_cost=cost,
                     priority=priority, covered_by_transfer=0, abc_class="A")


def row(sku, price, branch="B1"):
    return {"branch": branch, "sku": sku, "price": price, "lead_time_days": 5, "segment": ""}


def test_sell_probabilities_match_poisson():
    mean = 2.0
    p0 = math.exp(-mean)
    expected = [1 - p0, 1 - p0 * (1 + 2), 1 - p0 * (1 + 2 + 2)]
    assert budget_plan.sell_probabilities(mean, 0, 3) == pytest.approx(expected)
    # With 2 on hand, the first unit bought is the 3rd unit of demand.
    assert budget_plan.sell_probabilities(mean, 2, 1) == pytest.approx([expected[2]])
    assert budget_plan.sell_probabilities(0, 0, 2) == [0.0, 0.0]
    # Large means use the normal approximation and stay sensible.
    big = budget_plan.sell_probabilities(1000, 1000, 3)
    assert 0.45 < big[0] < 0.55 and big[0] > big[1] > big[2]


def test_hand_worked_example():
    # Horizon 10 days. "fast" sells 1/day: its first units almost surely sell.
    # "slow" sells 0.05/day: its unit sells with P = 1 - e^-0.5 = 0.39.
    # Profit per unit: fast 10 - 8 = 2, slow 60 - 40 = 20.
    orders = [PurchaseOrder("B1", "S", [line("fast", 3, 8, 1.0), line("slow", 1, 40, 0.05)])]
    rows = [row("fast", 10), row("slow", 60)]
    p_fast = budget_plan.sell_probabilities(10, 0, 3)
    p_slow = 1 - math.exp(-0.5)
    # Budget 40: either the slow unit (value 20 * 0.39 = 7.9) or the 3 fast units (2 * ~3 = 6.0, cost 24).
    plan = budget_plan.plan(orders, rows, SETTINGS, budget=40)
    chosen = {c.sku: c.qty for c in plan.candidates}
    assert chosen == {"fast": 0, "slow": 1}
    assert plan.avoided == pytest.approx(20 * p_slow)
    # Budget 64 buys everything.
    plan = budget_plan.plan(orders, rows, SETTINGS, budget=64)
    assert plan.status == "covers_all" and plan.avoided == pytest.approx(20 * p_slow + 2 * sum(p_fast))


def brute_force(candidates, budget, min_order):
    best = 0.0
    for qtys in itertools.product(*[range(c.suggested + 1) for c in candidates]):
        cost = sum(q * c.unit_cost for q, c in zip(qtys, candidates))
        if cost > budget + 1e-9:
            continue
        per_supplier = {}
        for q, c in zip(qtys, candidates):
            per_supplier[c.supplier] = per_supplier.get(c.supplier, 0) + q * c.unit_cost
        if any(budget_plan.min_order_applies(s, min_order) and 0 < v < min_order for s, v in per_supplier.items()):
            continue
        best = max(best, sum(sum(c.unit_values[:q]) for q, c in zip(qtys, candidates)))
    return best


@pytest.mark.parametrize("seed", range(25))
def test_solver_matches_brute_force(seed):
    rng = random.Random(seed)
    lines_by_supplier = {"S1": [], "S2": []}
    rows = []
    for index in range(4):
        sku = f"i{index}"
        cost = rng.choice([5, 8, 12, 20, 35])
        lines_by_supplier[rng.choice(["S1", "S2"])].append(line(sku, rng.randint(1, 4), cost, rng.uniform(0.05, 1.2), stock=rng.randint(0, 3)))
        rows.append(row(sku, cost * rng.uniform(1.05, 1.8)))
    orders = [PurchaseOrder("B1", s, lines) for s, lines in lines_by_supplier.items() if lines]
    budget = rng.choice([15, 30, 60, 90])
    min_order = rng.choice([0, 0, 25, 50])
    plan = budget_plan.plan(orders, rows, AnalysisSettings(lead_time_days=5, target_cover_days=5, customer_loss_percent=20), budget, min_order)
    assert plan.spent <= budget + 1e-9
    assert plan.avoided == pytest.approx(brute_force(plan.candidates, budget, min_order), abs=1e-6)
    # Never worse than buying in priority order with the same money.
    assert plan.avoided >= plan.priority_avoided - 1e-9


def test_minimum_order_skips_a_supplier_that_cannot_reach_it():
    orders = [PurchaseOrder("B1", "Small", [line("a", 2, 10, 1.0)]), PurchaseOrder("B1", "Big", [line("b", 10, 10, 1.0)])]
    rows = [row("a", 30), row("b", 12)]
    plan = budget_plan.plan(orders, rows, SETTINGS, budget=1000, min_order=50)
    chosen = {c.sku: c.qty for c in plan.candidates}
    assert chosen["a"] == 0 and chosen["b"] == 10
    assert plan.skipped_suppliers == [("B1", "Small", 20.0)]


def test_lines_without_supplier_ignore_the_minimum():
    orders = [PurchaseOrder("B1", "", [line("a", 2, 10, 1.0)])]
    plan = budget_plan.plan(orders, [row("a", 30)], SETTINGS, budget=1000, min_order=500)
    assert plan.candidates[0].qty == 2


def test_zero_budget_buys_nothing_and_empty_orders():
    orders = [PurchaseOrder("B1", "S", [line("a", 2, 10, 1.0)])]
    plan = budget_plan.plan(orders, [row("a", 30)], SETTINGS, budget=0)
    assert plan.spent == 0 and plan.postponed and not plan.bought
    assert budget_plan.plan([], [], SETTINGS, budget=100).status == "empty"


def test_customer_loss_favors_items_that_surely_sell():
    # "sure" sells 2/day, so its unit is almost certain to sell, for a profit of 1.
    # "maybe" sells with P = 0.6 for a profit of 2. Without customer loss: 1 < 1.2, buy "maybe".
    # Counting a lost customer at 100% of the price: 12 * 1.0 > 14 * 0.6, buy "sure".
    daily_maybe = -math.log(0.4) / 10
    orders = [PurchaseOrder("B1", "S", [line("sure", 1, 10, 2.0), line("maybe", 1, 10, daily_maybe)])]
    rows = [row("sure", 11), row("maybe", 12)]
    def pick(loss):
        settings = AnalysisSettings(lead_time_days=5, target_cover_days=5, customer_loss_percent=loss)
        return {c.sku for c in budget_plan.plan(orders, rows, settings, budget=10).bought}
    assert pick(0) == {"maybe"}
    assert pick(100) == {"sure"}
