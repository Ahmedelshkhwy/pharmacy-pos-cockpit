"""Merge cleaned records from any number of files into one item table per branch, then analyze it.

Input files can overlap (sales in one file, stock in another, cost in a third, batches with
expiry in a fourth). Items are joined by code, then barcode, then normalized name. When two
files provide the same measure for an item (e.g. stock on hand), the most specific file wins
instead of the values being added twice.
"""
from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from forecast import DayWeights, Forecast, forecast_item, to_day_index
from insights import analyze_profit_loss
from pos_pipeline import (
    classify_item,
    predictive_metrics,
    summarize,
    write_csv,
)
from settings import AnalysisSettings
from smart_import import Quality, TableSpec, iter_records, normalize_text

DEFAULT_BRANCH = "Main"

STOCK_MEASURES = ("qoh", "batches", "stock_value", "stock_cost_value")
SALES_MEASURES = ("qty_sold", "sales_value", "returns", "purchases")

ITEM_COLUMNS = [
    "ref", "barcode", "name", "category", "supplier", "branch",
    "purchases", "pos_sales", "returns_qty", "net_sales", "qoh", "sales_price", "cost", "cost_known",
    "stock_value", "cost_value", "gross_profit", "gross_profit_estimate", "turnover_ratio", "days_cover",
    "avg_daily_sales", "stockout_probability_7d", "stockout_probability_14d", "stockout_probability_30d",
    "reorder_point", "recommended_reorder_qty", "risk_score", "decision_label", "lost_sales_value",
    "lost_profit_value", "frozen_capital_value", "excess_qty", "excess_value", "negative_margin_loss",
    "expiry", "days_left", "expired_qty", "expiry_risk_qty", "expiry_risk_value",
    "base_daily_sales", "unavailable_days", "sales_trend", "trend_ratio", "lead_time_days",
    "unit_profit", "gross_margin_percent", "break_even_price", "max_safe_discount_percent",
    "suggested_offer_price", "offer_discount_percent", "offer_profit_per_unit", "offer_profit_if_sold",
    "signal_color", "tags",
]


def table_priority(spec: TableSpec, group: str) -> int:
    role = set(spec.role)
    sales_like = bool(role & {"transactions", "sales"})
    if group == "stock":
        if "stock" not in role:
            return 0
        return 3 if "expiry" in role else 2 if not sales_like else 1
    if group == "sales":
        return 3 if "transactions" in role else 2 if "sales" in role else 0
    if group == "cost":
        return 0 if "cost" not in role else 3 if not sales_like else 2
    if group == "price":
        return 0 if "price" not in role else 3 if not sales_like else 2 if "sales" in role else 1
    return 0


@dataclass
class Part:
    """What one table said about one item."""
    qty_sold: float = 0.0
    sales_value: float = 0.0
    sales_value_known: bool = False
    returns: float = 0.0
    purchases: float = 0.0
    has_sales: bool = False
    qoh: float = 0.0
    has_stock: bool = False
    batches: dict[str, float] = field(default_factory=dict)
    stock_value: float = 0.0
    stock_cost_value: float = 0.0
    cost_sum: float = 0.0
    cost_weight: float = 0.0
    price: float = 0.0
    rows: int = 0
    daily: dict[date, float] = field(default_factory=dict)  # invoice files only


@dataclass
class ItemAcc:
    ref: str = ""
    barcode: str = ""
    name: str = ""
    category: str = ""
    supplier: str = ""
    parts: dict[int, Part] = field(default_factory=dict)


@dataclass
class BranchAcc:
    name: str
    items: list[ItemAcc] = field(default_factory=list)
    by_ref: dict[str, ItemAcc] = field(default_factory=dict)
    by_barcode: dict[str, ItemAcc] = field(default_factory=dict)
    by_name: dict[str, ItemAcc] = field(default_factory=dict)
    first_tx: date | None = None
    last_tx: date | None = None
    tables: set[int] = field(default_factory=set)


@lru_cache(maxsize=200_000)
def name_key(name: str) -> str:
    return normalize_text(name).replace(" ", "")


def clean_branch_label(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:60]


def branch_slug(branch: str) -> str:
    return re.sub(r"[^\w\-]+|_", "-", branch, flags=re.UNICODE).strip("-")[:40] or "branch"


class Merger:
    def __init__(self, default_branch: str = "") -> None:
        self.default_branch = clean_branch_label(default_branch)
        self.branches: dict[str, BranchAcc] = {}
        self.specs: dict[int, TableSpec] = {}
        self.quality = Quality()
        self.tx_seen: set[int] = set()
        self.row_seen: set[int] = set()

    def branch_for(self, spec: TableSpec, record: dict[str, Any]) -> BranchAcc:
        label = clean_branch_label(record.get("branch") or "") or self.default_branch or clean_branch_label(spec.meta.get("branch_hint", "")) or DEFAULT_BRANCH
        if label not in self.branches:
            self.branches[label] = BranchAcc(name=label)
        return self.branches[label]

    def find_item(self, branch: BranchAcc, record: dict[str, Any]) -> ItemAcc:
        ref = str(record.get("ref") or "").strip().upper()
        barcode = str(record.get("barcode") or "").strip()
        nkey = name_key(record.get("name") or "")
        item = (ref and branch.by_ref.get(ref)) or (barcode and branch.by_barcode.get(barcode)) or (nkey and branch.by_name.get(nkey)) or None
        if item is None:
            item = ItemAcc()
            branch.items.append(item)
        if ref:
            branch.by_ref.setdefault(ref, item)
            item.ref = item.ref or str(record.get("ref")).strip()
        if barcode:
            branch.by_barcode.setdefault(barcode, item)
            item.barcode = item.barcode or barcode
        if nkey:
            branch.by_name.setdefault(nkey, item)
            item.name = item.name or record.get("name") or ""
        if record.get("category") and not item.category:
            item.category = record["category"]
        if record.get("supplier") and not item.supplier:
            item.supplier = record["supplier"]
        return item

    def add_table(self, path: Path, spec: TableSpec) -> None:
        table_id = len(self.specs)
        self.specs[table_id] = spec
        transactional = "transactions" in spec.role
        quality = Quality()
        for record in iter_records(path, spec, quality):
            if transactional and record.get("invoice"):
                tx_key = hash((record.get("invoice"), record.get("ref"), record.get("barcode"), record.get("name"), record.get("qty_sold"), record.get("price"), record.get("date"), record.get("branch")))
                if tx_key in self.tx_seen:
                    quality.flag("duplicate_transaction", f"#{record['_row']}: {record.get('invoice')} {record.get('name') or record.get('ref')}")
                    quality.rows_used -= 1
                    continue
                self.tx_seen.add(tx_key)
            elif not transactional:
                signature = hash((table_id, tuple(sorted((k, str(v)) for k, v in record.items() if k != "_row"))))
                if signature in self.row_seen:
                    quality.flag("duplicate_row", f"#{record['_row']}: {record.get('name') or record.get('ref')}")
                    quality.rows_used -= 1
                    continue
                self.row_seen.add(signature)
            branch = self.branch_for(spec, record)
            branch.tables.add(table_id)
            item = self.find_item(branch, record)
            part = item.parts.get(table_id)
            if part is None:
                part = item.parts[table_id] = Part()
            elif not transactional and not record.get("batch"):
                expiry = record.get("expiry")
                if expiry is None or expiry.isoformat() in part.batches:
                    quality.flag("duplicate_item", f"#{record['_row']}: {record.get('name') or record.get('ref')}")
            part.rows += 1
            self.apply_record(branch, part, record, transactional, quality)
        self.quality.merge(quality)

    @staticmethod
    def apply_record(branch: BranchAcc, part: Part, record: dict[str, Any], transactional: bool, quality: Quality) -> None:
        qty = record.get("qty_sold")
        value = record.get("sales_value")
        price = record.get("price")
        if qty is not None or value is not None:
            part.has_sales = True
        if transactional:
            when = record.get("date")
            if when:
                branch.first_tx = when if branch.first_tx is None or when < branch.first_tx else branch.first_tx
                branch.last_tx = when if branch.last_tx is None or when > branch.last_tx else branch.last_tx
            if qty is not None and qty < 0:
                part.returns += -qty
                quality.flag("return_lines")
            if when and qty is not None:
                part.daily[when] = part.daily.get(when, 0.0) + qty
        if qty is not None:
            part.qty_sold += qty
        if value is not None:
            part.sales_value += value
            part.sales_value_known = True
        elif transactional and qty is not None and price:
            part.sales_value += qty * price
            part.sales_value_known = True
        if record.get("customer_returns"):
            part.returns += abs(record["customer_returns"])
        if record.get("purchases"):
            part.purchases += record["purchases"]
        qoh = record.get("qoh")
        if qoh is not None:
            part.has_stock = True
            part.qoh += qoh
            expiry = record.get("expiry")
            key = expiry.isoformat() if isinstance(expiry, date) else ""
            part.batches[key] = part.batches.get(key, 0.0) + qoh
        elif record.get("expiry") and not part.batches:
            part.batches[record["expiry"].isoformat()] = 0.0
        if record.get("stock_value") is not None:
            part.stock_value += record["stock_value"]
        if record.get("stock_cost_value") is not None:
            part.stock_cost_value += record["stock_cost_value"]
        cost = record.get("cost")
        if cost is not None and cost > 0:
            weight = max(qoh or 0, 0) or 1.0
            part.cost_sum += cost * weight
            part.cost_weight += weight
        if price is not None and price > 0:
            part.price = price


def pick(item: ItemAcc, specs: dict[int, TableSpec], group: str, has: Any) -> Part | None:
    best: tuple[int, int] | None = None
    chosen = None
    for table_id, part in item.parts.items():
        if not has(part):
            continue
        rank = (table_priority(specs[table_id], group), -table_id)
        if best is None or rank > best:
            best, chosen = rank, part
    return chosen


def fefo_expiry_risk(batches: dict[str, float], avg_daily_sales: float, today: date, horizon_days: int = 180) -> tuple[float, float, str, int | None]:
    """Sell oldest-expiry first at the current velocity; units left when a batch expires are at risk."""
    dated = sorted((date.fromisoformat(key), qty) for key, qty in batches.items() if key)
    expired_qty = at_risk = 0.0
    consumed = 0.0
    nearest = ""
    for expiry, qty in dated:
        if qty <= 0:
            continue
        if not nearest:
            nearest = expiry.isoformat()
        days_left = (expiry - today).days
        if days_left <= 0:
            expired_qty += qty
            continue
        sellable = max(avg_daily_sales * days_left - consumed, 0.0)
        sold = min(qty, sellable)
        consumed += sold
        if days_left <= horizon_days:
            at_risk += qty - sold
    if not nearest and dated:
        nearest = dated[0][0].isoformat()
    days_left = (date.fromisoformat(nearest) - today).days if nearest else None
    return expired_qty, at_risk, nearest, days_left


def finalize_item(
    item: ItemAcc,
    branch: str,
    specs: dict[int, TableSpec],
    period_days: int,
    coverage: dict[str, bool],
    today: date,
    settings: AnalysisSettings,
    weights: DayWeights | None = None,
) -> dict[str, Any]:
    sales = pick(item, specs, "sales", lambda p: p.has_sales)
    stock = pick(item, specs, "stock", lambda p: p.has_stock)
    cost_part = pick(item, specs, "cost", lambda p: p.cost_weight > 0 or p.stock_cost_value > 0)
    price_part = pick(item, specs, "price", lambda p: p.price > 0)
    expiry_part = stock if stock and any(stock.batches) else pick(item, specs, "stock", lambda p: any(k for k in p.batches))

    pos_sales = max(sales.qty_sold, 0.0) if sales else 0.0
    returns_qty = sales.returns if sales else 0.0
    purchases = sales.purchases if sales else 0.0
    qoh_raw = stock.qoh if stock else 0.0
    qoh = max(qoh_raw, 0.0)
    price = price_part.price if price_part else 0.0
    net_sales = sales.sales_value if sales and sales.sales_value_known else pos_sales * price
    if price <= 0 and pos_sales > 0 and net_sales > 0:
        price = net_sales / pos_sales
    cost = 0.0
    if cost_part:
        if cost_part.cost_weight > 0:
            cost = cost_part.cost_sum / cost_part.cost_weight
        elif cost_part.stock_cost_value > 0 and cost_part.qoh > 0:
            cost = cost_part.stock_cost_value / cost_part.qoh
    cost_known = cost > 0
    stock_value = (stock.stock_value if stock and stock.stock_value > 0 else 0.0) or qoh * price
    cost_value = (stock.stock_cost_value if stock and stock.stock_cost_value > 0 else 0.0) or qoh * cost
    turnover_ratio = pos_sales / qoh if qoh > 0 else 0.0

    # Invoice files give a day-by-day history: estimate demand without stockout days and with
    # more weight on recent days. Summary files only give a total, so use the plain average.
    base_rate = pos_sales / period_days if period_days > 0 else 0.0
    forecast = Forecast(rate=base_rate, base_rate=base_rate, available_days=period_days, unavailable_days=0)
    if weights is not None and sales is not None and sales.daily:
        forecast = forecast_item(
            to_day_index(sales.daily, weights.start),
            weights,
            qoh=qoh_raw if coverage["stock"] else None,
            correct_stockouts=settings.correct_stockouts,
        )
    lead_time = settings.lead_time_for(item.supplier, item.category)
    metrics = predictive_metrics(
        pos_sales=pos_sales,
        qoh=qoh,
        sales_price=price,
        cost=cost,
        stock_value=stock_value,
        cost_value=cost_value,
        turnover_ratio=turnover_ratio,
        period_days=period_days,
        avg_daily_sales=forecast.rate,
        lead_time_days=lead_time,
        target_cover_days=settings.target_cover_days,
        overstock_cover_days=settings.overstock_cover_days,
        safety_z=settings.safety_z,
    )
    avg_daily = float(metrics["avg_daily_sales"])
    if not coverage["stock"]:
        for key in ("stockout_probability_7d", "stockout_probability_14d", "stockout_probability_30d", "reorder_point",
                    "recommended_reorder_qty", "lost_sales_value", "frozen_capital_value", "offer_profit_if_sold"):
            metrics[key] = 0
        metrics["days_cover"] = None
    if not coverage["sales"]:
        metrics["frozen_capital_value"] = 0
        metrics["days_cover"] = None

    tags: list[str] = []
    if coverage["sales"] and coverage["stock"]:
        tags = classify_item(
            pos_sales=pos_sales,
            qoh=qoh,
            turnover_ratio=turnover_ratio,
            days_cover=metrics["days_cover"],
            slow_cover_days=settings.slow_moving_cover_days,
            overstock_cover_days=settings.overstock_cover_days,
            fast_min_units=settings.fast_min_units,
        )
    elif coverage["sales"] and pos_sales > 0:
        tags = []

    unit_value = cost if cost_known else price
    expired_qty, risk_qty, nearest_expiry, days_left = fefo_expiry_risk(
        expiry_part.batches if expiry_part else {}, avg_daily, today, settings.expiry_horizon_days
    )
    if expired_qty > 0:
        tags.append("expired")
    if days_left is not None and 0 < days_left <= settings.near_expiry_days and qoh > 0:
        tags.append("near_expiry")
    if risk_qty > 0:
        tags.append("expiry_risk")

    unit_profit = price - cost if cost_known and price > 0 else 0.0
    gross_profit = net_sales - pos_sales * cost if cost_known else 0.0
    negative_margin_loss = max(pos_sales * cost - net_sales, 0.0) if cost_known and pos_sales > 0 and net_sales > 0 else 0.0
    if negative_margin_loss > 0:
        tags.append("negative_margin")
    expected_lost_units = max(avg_daily * 30 - qoh, 0.0) if coverage["stock"] else 0.0
    lost_profit_value = expected_lost_units * max(unit_profit, 0.0)
    excess_qty = max(qoh - avg_daily * settings.excess_cover_days, 0.0) if coverage["sales"] and coverage["stock"] and pos_sales > 0 else 0.0
    excess_value = excess_qty * unit_value

    decision = str(metrics["decision_label"])
    risk_score = int(metrics["risk_score"])
    if not coverage["stock"] or not coverage["sales"]:
        decision = "Healthy"
        risk_score = 1
    if negative_margin_loss > 0:
        decision = "Fix price: selling below cost"
        risk_score = min(10, risk_score + 3)
    if risk_qty > 0:
        decision = "Sell or return before expiry"
        risk_score = min(10, risk_score + 2)
    if expired_qty > 0:
        decision = "Remove expired stock"
        risk_score = min(10, risk_score + 3)
    metrics["risk_score"] = risk_score
    metrics["decision_label"] = decision
    metrics["signal_color"] = "red" if risk_score >= 8 else "orange" if risk_score >= 5 else "green"

    row = {
        "ref": item.ref or item.barcode or item.name[:40],
        "barcode": item.barcode,
        "name": item.name or item.ref or item.barcode,
        "category": item.category or "UNCATEGORIZED",
        "supplier": item.supplier,
        "branch": branch,
        "purchases": purchases,
        "pos_sales": round(pos_sales, 3),
        "returns_qty": round(returns_qty, 3),
        "net_sales": round(net_sales, 2),
        "qoh": round(qoh_raw, 3),
        "sales_price": round(price, 4),
        "cost": round(cost, 4),
        "cost_known": int(cost_known),
        "stock_value": round(stock_value, 2),
        "cost_value": round(cost_value, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_profit_estimate": round(gross_profit, 2),
        "turnover_ratio": round(turnover_ratio, 4),
        "lost_profit_value": round(lost_profit_value, 2),
        "excess_qty": round(excess_qty, 2),
        "excess_value": round(excess_value, 2),
        "negative_margin_loss": round(negative_margin_loss, 2),
        "expiry": nearest_expiry,
        "days_left": days_left,
        "expired_qty": round(expired_qty, 2),
        "expiry_risk_qty": round(risk_qty + expired_qty, 2),
        "expiry_risk_value": round((risk_qty + expired_qty) * unit_value, 2),
        "base_daily_sales": round(forecast.base_rate, 4),
        "unavailable_days": forecast.unavailable_days,
        "sales_trend": forecast.trend,
        "trend_ratio": forecast.trend_ratio,
        "lead_time_days": lead_time,
        "tags": " | ".join(tags) if tags else "normal",
    }
    row.update(metrics)
    row["days_cover"] = metrics["days_cover"]
    return row


@dataclass
class BranchResult:
    branch: str
    output_dir: Path
    item_count: int
    summary: dict[str, Any]


@dataclass
class ImportResult:
    branches: list[BranchResult]
    quality: dict[str, Any]


def resolve_period(branch: BranchAcc, specs: dict[int, TableSpec], override: int | None) -> tuple[int | None, str, str, str]:
    """Return (days, source, start, end). Dates found in the data beat what the user typed."""
    if branch.first_tx and branch.last_tx:
        return (branch.last_tx - branch.first_tx).days + 1, "transactions", branch.first_tx.isoformat(), branch.last_tx.isoformat()
    for table_id in sorted(branch.tables):
        meta = specs[table_id].meta
        if meta.get("start_date") and meta.get("end_date") and {"sales", "transactions"} & set(specs[table_id].role):
            start, end = date.fromisoformat(meta["start_date"]), date.fromisoformat(meta["end_date"])
            return (end - start).days + 1, "report_header", meta["start_date"], meta["end_date"]
    for table_id in sorted(branch.tables):
        if specs[table_id].meta.get("period_days"):
            return int(specs[table_id].meta["period_days"]), "column_header", "", ""
    if override:
        return override, "user", "", ""
    return None, "missing", "", ""


def data_coverage(branch: BranchAcc, specs: dict[int, TableSpec]) -> dict[str, bool]:
    roles = set().union(*(set(specs[t].role) for t in branch.tables)) if branch.tables else set()
    fields = set().union(*({c.field for c in specs[t].columns} for t in branch.tables)) if branch.tables else set()
    return {
        "sales": bool(roles & {"sales", "transactions"}),
        "stock": "stock" in roles,
        "cost": bool(fields & {"cost", "stock_cost_value"}),
        "price": "price" in fields or "sales_value" in fields,
        "expiry": "expiry" in fields,
        "category": "category" in fields,
    }


def item_quality(items: list[dict[str, Any]], coverage: dict[str, bool], quality: Quality) -> None:
    for item in items:
        label = f"{item['name']} ({item['ref']})"
        if item["pos_sales"] > 0 and item["sales_price"] <= 0:
            quality.flag("zero_price", label)
        if item["cost_known"] and item["sales_price"] > 0 and item["sales_price"] < item["cost"]:
            quality.flag("price_below_cost", label)
        if coverage["cost"] and not item["cost_known"] and item["qoh"] > 0:
            quality.flag("missing_cost", label)


def quality_score(quality: dict[str, Any], item_count: int, coverage: dict[str, bool]) -> int:
    counters = quality.get("counters", {})
    rows = max(quality.get("rows_read", 0), 1)
    items = max(item_count, 1)
    penalty = 0.0
    penalty += min((counters.get("bad_number", 0) + counters.get("bad_date", 0)) / rows, 1) * 60
    penalty += min(counters.get("negative_stock", 0) / rows, 1) * 30
    penalty += min(counters.get("price_below_cost", 0) / items, 1) * 25
    penalty += min(counters.get("zero_price", 0) / items, 1) * 25
    penalty += min(counters.get("missing_cost", 0) / items, 1) * 15
    penalty += min((counters.get("duplicate_row", 0) + counters.get("duplicate_item", 0) + counters.get("duplicate_transaction", 0)) / rows, 1) * 15
    if not coverage.get("cost"):
        penalty += 15
    if not coverage.get("price"):
        penalty += 10
    return int(max(0, min(100, round(100 - penalty))))


def run_smart_import(
    tables: Iterable[tuple[Path, TableSpec]],
    output_root: Path,
    default_branch: str = "",
    period_days_override: int | None = None,
    today: date | None = None,
    settings: AnalysisSettings | None = None,
) -> ImportResult:
    today = today or date.today()
    settings = settings or AnalysisSettings()
    merger = Merger(default_branch)
    sources = []
    for path, spec in tables:
        if not spec.usable:
            continue
        merger.add_table(path, spec)
        sources.append({"file": spec.file_name, "sheet": spec.sheet, "role": spec.role})
    if not merger.branches:
        raise ValueError("no_usable_rows")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results: list[BranchResult] = []
    overall_quality = merger.quality
    for branch_name, branch in sorted(merger.branches.items()):
        coverage = data_coverage(branch, merger.specs)
        period_days, period_source, start, end = resolve_period(branch, merger.specs, period_days_override)
        if coverage["sales"] and not period_days:
            raise ValueError("period_required")
        period = period_days or 30
        weights = DayWeights(branch.first_tx, period, settings.recency_half_life_days) if period_source == "transactions" else None
        items = [finalize_item(item, branch_name, merger.specs, period, coverage, today, settings, weights) for item in branch.items]
        branch_quality = Quality()
        item_quality(items, coverage, branch_quality)
        overall_quality.merge(branch_quality)

        metadata = {
            "source": "smart",
            "branch": branch_name,
            "start_date": start,
            "end_date": end or today.isoformat(),
            "period_days": str(period),
            "actual_export_period_days": str(period),
            "period_source": period_source,
            "report_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "settings": settings.to_dict(),
        }
        if period_days_override and period_source in {"transactions", "report_header"} and period_days_override != period:
            metadata["period_warning"] = f"user_period_{period_days_override}_file_period_{period}"
        summary = summarize(items, metadata)
        summary["coverage"] = coverage
        summary["sources"] = sources
        summary["profit_loss"] = analyze_profit_loss(items, period_days=period, coverage=coverage, settings=settings)
        summary["expiry_risk_value"] = round(sum(item["expiry_risk_value"] for item in items), 2)
        summary["gross_profit"] = round(sum(item["gross_profit"] for item in items), 2)

        output_dir = output_root / f"{branch_slug(branch_name)}_{stamp}"
        suffix = 2
        while output_dir.exists():
            output_dir = output_root / f"{branch_slug(branch_name)}_{stamp}_{suffix}"
            suffix += 1
        output_dir.mkdir(parents=True)
        write_csv(output_dir / "merged_items.csv", items, ITEM_COLUMNS)
        results.append(BranchResult(branch=branch_name, output_dir=output_dir, item_count=len(items), summary=summary))

    quality = overall_quality.to_dict()
    total_items = sum(result.item_count for result in results)
    combined_coverage = {key: any(result.summary["coverage"][key] for result in results) for key in results[0].summary["coverage"]}
    quality["score"] = quality_score(quality, total_items, combined_coverage)
    quality["coverage"] = combined_coverage
    for result in results:
        result.summary["quality"] = quality
        (result.output_dir / "summary.json").write_text(json.dumps(result.summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return ImportResult(branches=results, quality=quality)
