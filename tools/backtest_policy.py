"""Replay real daily sales and compare purchasing policies with the same money.

Every review day (weekly by default) the simulated store does what the tool would tell it:
forecast each item from the last 90 days of its own (simulated) sales, classify ABC by profit and
by quantity, compute the suggested orders, then buy according to the policy:

    unlimited   buy every suggested order (the reference: no money limit)
    priority    a fixed weekly budget, spent in the orders' priority (urgent first, bigger first)
    optimized   the same weekly budget, spent with the budget plan (budget_plan.plan)

Orders arrive after the lead time. Each day the real demand of that day is served from stock;
what cannot be served is lost. The weekly budget is a share of what the unlimited policy spends
on average, so "70%" means the store has 70% of the money the suggested orders would need.

Assumptions, printed with the results:
- Recorded sales are taken as demand. The real store's own stockouts are not visible in the data.
- The data has no purchase cost, so each item gets a fixed margin between --margin-min and --margin-max
  (the same item always gets the same margin).
- Items are the top --items by revenue in the 90 days before the simulation starts (no look-ahead).
- One location: transfers between branches are not part of this test.

Run:  python tools/backtest_policy.py sample_data/uci/online_retail_II.xlsx --report docs/backtest-results.md
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import budget_plan  # noqa: E402
from actions import purchase_orders  # noqa: E402
from forecast import DayWeights, forecast_item  # noqa: E402
from pos_pipeline import predictive_metrics  # noqa: E402
from settings import AnalysisSettings  # noqa: E402
from smart_pipeline import abc_classes, segment_for  # noqa: E402

HISTORY_DAYS = 90
BRANCH = "Store"


@dataclass
class Item:
    ref: str
    name: str
    price: float
    cost: float
    demand: dict[int, float]          # day index -> units


@dataclass
class Totals:
    demand_units: float = 0.0
    sold_units: float = 0.0
    demand_value: float = 0.0
    sold_value: float = 0.0
    lost_units: float = 0.0
    lost_sales: float = 0.0
    lost_profit: float = 0.0
    profit: float = 0.0
    stockout_days: int = 0            # item-days with demand that could not be fully served
    spent: float = 0.0
    inventory_sum: float = 0.0        # sum over days of stock at cost, for the average
    days: int = 0
    reviews: int = 0
    review_spend: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, float]:
        avg_inventory = self.inventory_sum / self.days if self.days else 0.0
        return {
            "fill_rate": self.sold_units / self.demand_units * 100 if self.demand_units else 100.0,
            "value_fill_rate": self.sold_value / self.demand_value * 100 if self.demand_value else 100.0,
            "lost_sales": self.lost_sales,
            "lost_profit": self.lost_profit,
            "stockout_days": self.stockout_days,
            "profit": self.profit,
            "spent": self.spent,
            "avg_inventory": avg_inventory,
            "gmroi": self.profit * 365 / self.days / avg_inventory if avg_inventory and self.days else 0.0,
        }


# ---------------------------------------------------------------- loading

def stable_margin(ref: str, low: float, high: float) -> float:
    digest = int(hashlib.sha256(ref.encode()).hexdigest()[:8], 16)
    return low + (high - low) * (digest / 0xFFFFFFFF)


def load_sales(path: Path) -> tuple[date, dict[str, dict[str, Any]]]:
    """Daily units and average price per item from any invoice file the tool can read. Cached next to the file."""
    cache = path.with_suffix(path.suffix + ".daily.json.gz")
    if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
        data = json.loads(gzip.decompress(cache.read_bytes()))
        return date.fromisoformat(data["start"]), data["items"]
    from smart_import import Quality, detect_file, iter_records

    daily: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    revenue: dict[str, float] = defaultdict(float)
    units: dict[str, float] = defaultdict(float)
    names: dict[str, str] = {}
    seen: set[int] = set()
    for spec in detect_file(path):
        if "transactions" not in spec.role:
            continue
        for record in iter_records(path, spec, Quality()):
            key = hash((record.get("invoice"), record.get("ref"), record.get("qty_sold"), record.get("price"), record.get("date")))
            if record.get("invoice") and key in seen:
                continue  # the same invoice line in two overlapping sheets
            seen.add(key)
            day, qty, price = record.get("date"), record.get("qty_sold"), record.get("price")
            ref = str(record.get("ref") or record.get("barcode") or record.get("name") or "")
            if not day or not ref or qty is None:
                continue
            daily[ref][day] += qty
            names.setdefault(ref, str(record.get("name") or ref))
            if qty > 0 and price and price > 0:
                revenue[ref] += qty * price
                units[ref] += qty
    start = min(day for days in daily.values() for day in days)
    items = {
        ref: {"name": names[ref], "price": revenue[ref] / units[ref],
              "daily": {str((day - start).days): qty for day, qty in days.items() if qty > 0}}
        for ref, days in daily.items() if units.get(ref)
    }
    cache.write_bytes(gzip.compress(json.dumps({"start": start.isoformat(), "items": items}).encode()))
    return start, items


def pick_items(raw: dict[str, dict[str, Any]], sim_start: int, count: int, margin: tuple[float, float]) -> list[Item]:
    """Top items by revenue in the 90 days before the simulation; product codes only (they contain a digit)."""
    window = range(sim_start - HISTORY_DAYS, sim_start)
    scored = []
    for ref, data in raw.items():
        if not any(ch.isdigit() for ch in ref):
            continue  # postage, fees, manual adjustments
        units = sum(qty for day, qty in data["daily"].items() if int(day) in window)
        if units > 0:
            scored.append((units * data["price"], ref))
    chosen = [ref for _score, ref in sorted(scored, reverse=True)[:count]]
    items = []
    for ref in chosen:
        data = raw[ref]
        price = data["price"]
        items.append(Item(ref=ref, name=data["name"], price=price, cost=price * (1 - stable_margin(ref, *margin)),
                          demand={int(day): qty for day, qty in data["daily"].items()}))
    return items


# ---------------------------------------------------------------- one review

def review_rows(items: list[Item], stock: list[float], sales: list[dict[int, float]], today: int,
                settings: AnalysisSettings, start: date) -> list[dict[str, Any]]:
    """What the tool would see and suggest on this review day: one row per item, like the items table."""
    window_start = today - HISTORY_DAYS
    weights = DayWeights(start + timedelta(days=window_start), HISTORY_DAYS, settings.recency_half_life_days)
    first = []
    rates = []
    for index, item in enumerate(items):
        history = {day - window_start: qty for day, qty in sales[index].items() if window_start <= day < today}
        rate = forecast_item(history, weights, qoh=stock[index], correct_stockouts=settings.correct_stockouts).rate if history else 0.0
        rates.append(rate)
        sold = sum(history.values())
        first.append({"pos_sales": sold, "gross_profit": sold * (item.price - item.cost)})
    profit_classes = abc_classes(first, "gross_profit")
    volume_classes = abc_classes(first, "pos_sales")
    rows = []
    for index, item in enumerate(items):
        on_hand = max(stock[index], 0.0)
        metrics = predictive_metrics(
            pos_sales=first[index]["pos_sales"], qoh=on_hand, sales_price=item.price, cost=item.cost,
            stock_value=on_hand * item.price, cost_value=on_hand * item.cost, turnover_ratio=0.0,
            period_days=HISTORY_DAYS, avg_daily_sales=rates[index], lead_time_days=settings.lead_time_days,
            target_cover_days=settings.target_cover_days, overstock_cover_days=settings.overstock_cover_days,
            safety_z=settings.safety_z(profit_classes[index], volume_classes[index]),
        )
        rows.append({
            "branch": BRANCH, "sku": item.ref, "barcode": "", "name": item.name, "supplier": "", "tags": "",
            "stock": on_hand, "avg_daily_sales": rates[index], "recommended_reorder_qty": metrics["recommended_reorder_qty"],
            "stockout_probability_7d": metrics["stockout_probability_7d"], "stockout_probability_14d": metrics["stockout_probability_14d"],
            "cost_price": item.cost, "price": item.price, "abc_class": profit_classes[index],
            "segment": segment_for(profit_classes[index], volume_classes[index]), "lead_time_days": settings.lead_time_days,
        })
    return rows


def order_quantities(policy: str, rows: list[dict[str, Any]], settings: AnalysisSettings, budget: float) -> dict[str, int]:
    orders = purchase_orders(rows, [])
    if policy == "unlimited":
        return {line.sku: line.qty for order in orders for line in order.lines}
    if policy == "priority":
        candidates = budget_plan.build_candidates(orders, rows, settings)
        return {c.sku: q for c, q in zip(candidates, budget_plan.priority_order_quantities(candidates, budget, 0.0))}
    if policy == "optimized":
        return {c.sku: c.qty for c in budget_plan.plan(orders, rows, settings, budget).candidates}
    raise ValueError(policy)


# ---------------------------------------------------------------- simulation

def simulate(items: list[Item], start: date, sim_start: int, sim_end: int, settings: AnalysisSettings,
             policy: str, weekly_budget: float = 0.0, review_days: int = 7) -> Totals:
    """Run one policy from sim_start (inclusive) to sim_end (exclusive), day indexes from `start`."""
    totals = Totals()
    # Before the simulation the store sold what the data says; that is the history the first review sees.
    sales = [{day: qty for day, qty in item.demand.items() if day < sim_start} for item in items]
    # Same opening stock for every policy: about half an order cycle above the lead-time demand.
    stock = []
    for item, history in zip(items, sales):
        rate = sum(qty for day, qty in history.items() if day >= sim_start - HISTORY_DAYS) / HISTORY_DAYS
        stock.append(float(round(rate * (settings.lead_time_days + settings.target_cover_days / 2))))
    arrivals: dict[int, list[tuple[int, int]]] = defaultdict(list)
    sku_index = {item.ref: index for index, item in enumerate(items)}
    budget = weekly_budget * review_days / 7
    for today in range(sim_start, sim_end):
        for index, qty in arrivals.pop(today, []):
            stock[index] += qty
        if (today - sim_start) % review_days == 0:
            rows = review_rows(items, stock, sales, today, settings, start)
            spent = 0.0
            for sku, qty in order_quantities(policy, rows, settings, budget).items():
                if qty > 0:
                    index = sku_index[sku]
                    arrivals[today + settings.lead_time_days].append((index, qty))
                    spent += qty * items[index].cost
            totals.spent += spent
            totals.review_spend.append(spent)
            totals.reviews += 1
        for index, item in enumerate(items):
            demand = max(item.demand.get(today, 0.0), 0.0)
            if demand:
                sold = min(demand, stock[index])
                stock[index] -= sold
                if sold:
                    sales[index][today] = sold
                lost = demand - sold
                totals.demand_units += demand
                totals.sold_units += sold
                totals.demand_value += demand * item.price
                totals.sold_value += sold * item.price
                totals.profit += sold * (item.price - item.cost)
                if lost > 0:
                    totals.lost_units += lost
                    totals.lost_sales += lost * item.price
                    totals.lost_profit += lost * (item.price - item.cost)
                    totals.stockout_days += 1
            totals.inventory_sum += stock[index] * item.cost
        totals.days += 1
    return totals


def run(path: Path, items_count: int, sim_from: str | None, sim_days: int | None, ratios: list[float],
        settings: AnalysisSettings, margin: tuple[float, float], log=print) -> dict[str, Any]:
    started = time.time()
    start, raw = load_sales(path)
    last = max(int(day) for data in raw.values() for day in data["daily"])
    sim_start = (date.fromisoformat(sim_from) - start).days if sim_from else HISTORY_DAYS
    sim_end = min(last + 1, sim_start + sim_days) if sim_days else last + 1
    items = pick_items(raw, sim_start, items_count, margin)
    log(f"{len(items)} items, {start + timedelta(days=sim_start)} to {start + timedelta(days=sim_end - 1)} "
        f"({sim_end - sim_start} days), loaded in {time.time() - started:.0f}s")
    results: dict[str, Any] = {"assumptions": {
        "file": path.name, "items": len(items), "from": str(start + timedelta(days=sim_start)),
        "to": str(start + timedelta(days=sim_end - 1)), "days": sim_end - sim_start,
        "lead_time_days": settings.lead_time_days, "target_cover_days": settings.target_cover_days,
        "customer_loss_percent": settings.customer_loss_percent, "margin": list(margin),
    }, "runs": []}
    base = simulate(items, start, sim_start, sim_end, settings, "unlimited")
    weekly = base.spent / max(base.reviews, 1)
    results["unlimited"] = base.summary() | {"weekly_spend": weekly}
    log(f"unlimited: fill {results['unlimited']['fill_rate']:.1f}%, weekly spend {weekly:,.0f}")
    for ratio in ratios:
        row = {"ratio": ratio, "weekly_budget": weekly * ratio}
        for policy in ("priority", "optimized"):
            row[policy] = simulate(items, start, sim_start, sim_end, settings, policy, weekly * ratio).summary()
        results["runs"].append(row)
        log(f"{ratio:.0%}: lost profit priority {row['priority']['lost_profit']:,.0f} vs optimized {row['optimized']['lost_profit']:,.0f}")
    results["seconds"] = round(time.time() - started)
    return results


def report(results: dict[str, Any]) -> str:
    a = results["assumptions"]
    u = results["unlimited"]
    lines = [
        "# Backtest: buying with a limited budget",
        "",
        f"Real daily sales from `{a['file']}`: {a['items']} items, {a['from']} to {a['to']} ({a['days']} days).",
        f"Weekly review, lead time {a['lead_time_days']} days, order cover {a['target_cover_days']} days, "
        f"customer loss {a['customer_loss_percent']:g}% of the price, margins {a['margin'][0]:.0%} to {a['margin'][1]:.0%} (assumed; the data has no cost).",
        "",
        f"Without a money limit the suggested orders cost **{u['weekly_spend']:,.0f} a week** on average and serve "
        f"**{u['fill_rate']:.1f}%** of demand.",
        "",
        "| Budget (share of that) | Policy | Spent | Demand served (value) | Demand served (units) | Lost profit | Lost sales | Stockout item-days | Avg stock at cost | GMROI |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    names = {"priority": "Priority order", "optimized": "Budget plan"}
    for run_ in results["runs"]:
        for policy in ("priority", "optimized"):
            s = run_[policy]
            lines.append(f"| {run_['ratio']:.0%} ({run_['weekly_budget']:,.0f}/week) | {names[policy]} | {s['spent']:,.0f} | {s['value_fill_rate']:.1f}% | {s['fill_rate']:.1f}% | "
                         f"{s['lost_profit']:,.0f} | {s['lost_sales']:,.0f} | {s['stockout_days']:,} | {s['avg_inventory']:,.0f} | {s['gmroi']:.2f} |")
    lines += ["", "## Difference with the same money", ""]
    for run_ in results["runs"]:
        p, o = run_["priority"], run_["optimized"]
        saved = p["lost_profit"] - o["lost_profit"]
        share = saved / p["lost_profit"] * 100 if p["lost_profit"] else 0.0
        lines.append(f"- **{run_['ratio']:.0%} budget:** the budget plan lost {o['lost_profit']:,.0f} of profit to stockouts against "
                     f"{p['lost_profit']:,.0f} buying in priority order ({abs(share):.1f}% {'less' if share >= 0 else 'more'}). "
                     f"It served {o['value_fill_rate']:.1f}% of demand by value against {p['value_fill_rate']:.1f}% "
                     f"({o['fill_rate']:.1f}% against {p['fill_rate']:.1f}% by units), "
                     f"with {o['stockout_days']:,} stockout item-days against {p['stockout_days']:,}.")
    lines += [
        "",
        "## Limits of this test",
        "",
        "- Recorded sales are taken as demand; days the real store was out of stock look like days without demand.",
        "- Purchase cost is assumed (fixed margin per item), so profits are illustrative; the comparison between policies uses the same assumption on both sides.",
        "- One location: transfers between branches are not tested here.",
        "- No expiry and no supplier minimum orders in this data.",
        "- The budget plan aims at value (profit plus customer loss), not at the number of units, so it can serve fewer units while losing less.",
        "",
        f"Run time: {results['seconds']}s. Reproduce with `python tools/backtest_policy.py <file> --report <path>`.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path)
    parser.add_argument("--items", type=int, default=300)
    parser.add_argument("--from", dest="sim_from", help="first simulated day (YYYY-MM-DD); default 90 days after the data starts")
    parser.add_argument("--days", type=int, help="simulated days; default until the data ends")
    parser.add_argument("--budgets", default="0.5,0.7,0.9", help="weekly budgets as shares of the unlimited weekly spend")
    parser.add_argument("--margin-min", type=float, default=0.15)
    parser.add_argument("--margin-max", type=float, default=0.40)
    parser.add_argument("--customer-loss", type=float, default=None, help="percent of price; default from settings (20)")
    parser.add_argument("--report", type=Path, help="write a Markdown report here")
    parser.add_argument("--json", type=Path, help="write the raw results here")
    args = parser.parse_args()
    settings = AnalysisSettings()
    if args.customer_loss is not None:
        settings.customer_loss_percent = args.customer_loss
    results = run(args.file, args.items, args.sim_from, args.days, [float(x) for x in args.budgets.split(",")],
                  settings, (args.margin_min, args.margin_max))
    text = report(results)
    print(text)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
