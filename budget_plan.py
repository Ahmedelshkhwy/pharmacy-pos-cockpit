"""Purchase plan within a budget: which units to buy when the money does not cover every order.

The suggested purchase orders (actions.purchase_orders) say what each branch needs. When the budget
is smaller than their total, this module picks the units that prevent the most expected loss per
unit of money, and leaves out suppliers whose order would fall under their minimum.

Value of one unit: the k-th unit bought only matters if demand over the order's horizon (lead time
plus order cover) goes past what is already on hand or coming by transfer plus k-1. So

    value(k) = P(demand > stock + k - 1) * (unit profit + customer loss)

with demand over the horizon taken as Poisson. Values fall as k grows, so the first units of an
urgent item are worth far more than the last units of a comfortable one.

The choice itself is a small mixed-integer program solved with HiGHS:
    maximize   sum value * units
    subject to sum cost * units <= budget
               each (branch, supplier) order is either 0 or at least the minimum order
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from actions import NO_SUPPLIER, PRIORITY_ORDER, PurchaseOrder, num
from settings import AnalysisSettings

MAX_CHUNKS = 12          # units of one line are grouped into at most this many blocks
SOLVER_TIME_LIMIT = 20.0  # seconds


def sell_probabilities(mean: float, on_hand: float, units: int) -> list[float]:
    """P(demand > on_hand + k - 1) for k = 1..units, demand ~ Poisson(mean)."""
    if units <= 0:
        return []
    if mean <= 0:
        return [0.0] * units
    start = max(int(math.floor(on_hand)), 0)
    if mean > 400:
        # Normal approximation with continuity correction; exact Poisson terms underflow here.
        sd = math.sqrt(mean)
        return [0.5 * math.erfc((start + k - 0.5 - mean) / (sd * math.sqrt(2))) for k in range(1, units + 1)]
    # Poisson cdf up to start + units - 1, built term by term.
    pmf = math.exp(-mean)
    cdf = 0.0
    probabilities = []
    for n in range(start + units):
        cdf += pmf
        if n >= start:
            probabilities.append(max(0.0, 1.0 - cdf))
        pmf *= mean / (n + 1)
    return probabilities


@dataclass
class Candidate:
    branch: str
    supplier: str
    sku: str
    name: str
    abc_class: str
    segment: str
    priority: str
    suggested: int
    unit_cost: float
    unit_loss: float
    unit_values: list[float]           # value of the 1st, 2nd, ... unit
    qty: int = 0

    @property
    def cost(self) -> float:
        return self.qty * self.unit_cost

    @property
    def avoided(self) -> float:
        return sum(self.unit_values[: self.qty])

    @property
    def full_value(self) -> float:
        return sum(self.unit_values)


@dataclass
class BudgetPlan:
    budget: float
    min_order: float
    candidates: list[Candidate]
    full_cost: float
    spent: float
    avoided: float
    full_avoided: float
    priority_spent: float
    priority_avoided: float
    skipped_suppliers: list[tuple[str, str, float]] = field(default_factory=list)  # (branch, supplier, order value)
    status: str = "optimal"            # optimal | time_limit | covers_all | empty

    @property
    def bought(self) -> list[Candidate]:
        return [c for c in self.candidates if c.qty > 0]

    @property
    def postponed(self) -> list[Candidate]:
        return [c for c in self.candidates if c.qty < c.suggested]

    @property
    def min_order_active(self) -> bool:
        """The minimum order applies to at least one order (lines without a supplier are exempt)."""
        return any(min_order_applies(c.supplier, self.min_order) for c in self.candidates)

    def supplier_skipped(self, candidate: Candidate) -> bool:
        """The whole order of this line's supplier and branch is under the minimum order."""
        return any(b == candidate.branch and s == candidate.supplier for b, s, _value in self.skipped_suppliers)

    def order_groups(self) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str], list[Candidate]] = {}
        for candidate in self.bought:
            groups.setdefault((candidate.branch, candidate.supplier), []).append(candidate)
        result = [{"branch": b, "supplier": s, "lines": sorted(lines, key=lambda c: -c.avoided), "value": sum(c.cost for c in lines)}
                  for (b, s), lines in groups.items()]
        return sorted(result, key=lambda g: (g["branch"], -g["value"]))


def build_candidates(orders: list[PurchaseOrder], rows: list[dict[str, Any]], settings: AnalysisSettings) -> list[Candidate]:
    by_key = {(row["branch"], row.get("sku") or ""): row for row in rows}
    candidates = []
    for order in orders:
        for line in order.lines:
            row = by_key.get((order.branch, line.sku), {})
            price = num(row.get("price"))
            cost = line.unit_cost
            if cost <= 0 or line.qty <= 0:
                continue
            profit = max(price - cost, 0.0) if price > 0 else 0.0
            customer_loss = settings.customer_loss_percent / 100 * (price if price > 0 else cost)
            horizon = (num(row.get("lead_time_days")) or settings.lead_time_days) + settings.target_cover_days
            probabilities = sell_probabilities(line.daily * horizon, max(line.stock, 0.0) + line.covered_by_transfer, line.qty)
            unit_loss = profit + customer_loss
            candidates.append(Candidate(
                branch=order.branch, supplier=order.supplier, sku=line.sku, name=line.name, abc_class=line.abc_class,
                segment=row.get("segment") or "", priority=line.priority, suggested=line.qty, unit_cost=cost,
                unit_loss=unit_loss, unit_values=[p * unit_loss for p in probabilities],
            ))
    return candidates


def chunks(values: list[float]) -> list[tuple[int, float]]:
    """Split a line's units into at most MAX_CHUNKS blocks: (units, value of one unit in the block)."""
    count = len(values)
    blocks = min(count, MAX_CHUNKS)
    sizes = [count // blocks + (1 if index < count % blocks else 0) for index in range(blocks)]
    result, start = [], 0
    for size in sizes:
        result.append((size, sum(values[start:start + size]) / size))
        start += size
    return result


def min_order_applies(supplier: str, min_order: float) -> bool:
    return min_order > 0 and supplier != NO_SUPPLIER


def solve(candidates: list[Candidate], budget: float, min_order: float) -> str:
    """Set candidate.qty to the best units within the budget. Returns the solver status."""
    import highspy

    model = highspy.Highs()
    model.setOptionValue("output_flag", False)
    model.setOptionValue("time_limit", SOLVER_TIME_LIMIT)
    model.setOptionValue("mip_rel_gap", 1e-6)
    blocks: list[tuple[int, Any, int]] = []   # (candidate index, variable, block size)
    objective, spend = [], []
    supplier_vars: dict[tuple[str, str], Any] = {}
    supplier_spend: dict[tuple[str, str], list[Any]] = {}
    for index, candidate in enumerate(candidates):
        key = (candidate.branch, candidate.supplier)
        gate = None
        if min_order_applies(candidate.supplier, min_order):
            if key not in supplier_vars:
                supplier_vars[key] = model.addIntegral(lb=0, ub=1)
            gate = supplier_vars[key]
        for size, value in chunks(candidate.unit_values):
            units = model.addIntegral(lb=0, ub=size)
            blocks.append((index, units, size))
            objective.append(value * units)
            spend.append(candidate.unit_cost * units)
            if gate is not None:
                model.addConstr(units <= size * gate)
                supplier_spend.setdefault(key, []).append(candidate.unit_cost * units)
    model.addConstr(sum(spend) <= budget)
    for key, gate in supplier_vars.items():
        model.addConstr(sum(supplier_spend[key]) >= min_order * gate)
    model.maximize(sum(objective))
    status = model.getModelStatus()
    for candidate in candidates:
        candidate.qty = 0
    if status not in (highspy.HighsModelStatus.kOptimal, highspy.HighsModelStatus.kTimeLimit):
        return "failed"
    for index, units, _size in blocks:
        candidates[index].qty += int(round(model.val(units)))
    return "optimal" if status == highspy.HighsModelStatus.kOptimal else "time_limit"


def priority_order_plan(candidates: list[Candidate], budget: float, min_order: float) -> tuple[float, float]:
    """What buying in the order's own priority (urgent, soon, planned; bigger first) would achieve
    with the same budget: (spent, avoided loss). Orders that end up under the minimum are dropped."""
    ranked = sorted(range(len(candidates)), key=lambda i: (PRIORITY_ORDER[candidates[i].priority], -candidates[i].suggested * candidates[i].unit_cost))
    qty = [0] * len(candidates)
    left = budget
    for index in ranked:
        candidate = candidates[index]
        units = min(candidate.suggested, int(left // candidate.unit_cost)) if candidate.unit_cost > 0 else 0
        qty[index] = max(units, 0)
        left -= qty[index] * candidate.unit_cost
    totals: dict[tuple[str, str], float] = {}
    for index, candidate in enumerate(candidates):
        totals[(candidate.branch, candidate.supplier)] = totals.get((candidate.branch, candidate.supplier), 0.0) + qty[index] * candidate.unit_cost
    spent = avoided = 0.0
    for index, candidate in enumerate(candidates):
        if min_order_applies(candidate.supplier, min_order) and totals[(candidate.branch, candidate.supplier)] < min_order:
            continue
        spent += qty[index] * candidate.unit_cost
        avoided += sum(candidate.unit_values[: qty[index]])
    return spent, avoided


def plan(orders: list[PurchaseOrder], rows: list[dict[str, Any]], settings: AnalysisSettings, budget: float, min_order: float = 0.0) -> BudgetPlan:
    candidates = build_candidates(orders, rows, settings)
    full_cost = sum(c.suggested * c.unit_cost for c in candidates)
    full_avoided = sum(c.full_value for c in candidates)
    totals: dict[tuple[str, str], float] = {}
    for candidate in candidates:
        totals[(candidate.branch, candidate.supplier)] = totals.get((candidate.branch, candidate.supplier), 0.0) + candidate.suggested * candidate.unit_cost
    if not candidates:
        status = "empty"
    elif full_cost <= budget and all(not min_order_applies(s, min_order) or value >= min_order for (_b, s), value in totals.items()):
        for candidate in candidates:
            candidate.qty = candidate.suggested
        status = "covers_all"
    else:
        status = solve(candidates, budget, min_order)
    spent = sum(c.cost for c in candidates)
    bought_keys = {(c.branch, c.supplier) for c in candidates if c.qty > 0}
    skipped = sorted(((b, s, value) for (b, s), value in totals.items()
                      if (b, s) not in bought_keys and min_order_applies(s, min_order) and value < min_order), key=lambda x: -x[2])
    priority_spent, priority_avoided = priority_order_plan(candidates, budget, min_order)
    candidates.sort(key=lambda c: (c.qty >= c.suggested, -(c.full_value - c.avoided)))
    return BudgetPlan(
        budget=budget, min_order=min_order, candidates=candidates, full_cost=full_cost, spent=spent,
        avoided=sum(c.avoided for c in candidates), full_avoided=full_avoided,
        priority_spent=priority_spent, priority_avoided=priority_avoided, skipped_suppliers=skipped, status=status,
    )
