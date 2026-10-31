"""Turn the analysis into a to-do list: what to transfer between branches, what to buy from
whom, and what to stop buying.

Works on item rows as stored in the app database (one row per item per branch).
Transfers are planned first; purchase orders only cover what transfers cannot.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from settings import AnalysisSettings
from smart_import import normalize_text

URGENT_PROBABILITY = 0.6
NO_SUPPLIER = ""


def num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def unit_value(row: dict[str, Any]) -> float:
    return num(row.get("cost_price")) or num(row.get("price"))


def target_stock(row: dict[str, Any], settings: AnalysisSettings) -> float:
    """Stock a branch should hold: reorder point plus the order cover (same rule as reorder quantity)."""
    return num(row.get("reorder_point")) + num(row.get("avg_daily_sales")) * settings.target_cover_days


def shortage(row: dict[str, Any], settings: AnalysisSettings) -> float:
    if num(row.get("avg_daily_sales")) <= 0:
        return 0.0
    return max(target_stock(row, settings) - max(num(row.get("stock")), 0.0), 0.0)


def surplus(row: dict[str, Any], settings: AnalysisSettings) -> float:
    """Units a branch can give away without going short itself. Expired units are never moved."""
    sellable = max(num(row.get("stock")), 0.0) - num(row.get("expired_qty"))
    if sellable <= 0:
        return 0.0
    keep = target_stock(row, settings) if num(row.get("avg_daily_sales")) > 0 else 0.0
    at_risk = max(num(row.get("expiry_risk_qty")) - num(row.get("expired_qty")), 0.0)
    return min(max(sellable - keep, at_risk, 0.0), sellable)


class _Groups:
    """Union-find over item identities so the same item matches across branches that use
    different code systems (barcode in one, internal code in another, name in a third)."""

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, node: int) -> int:
        while self.parent.setdefault(node, node) != node:
            self.parent[node] = self.parent[self.parent[node]]
            node = self.parent[node]
        return node

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def group_items(rows: list[dict[str, Any]]) -> list[list[int]]:
    groups = _Groups()
    first_seen: dict[str, int] = {}
    for index, row in enumerate(rows):
        groups.find(index)
        keys = []
        if row.get("barcode"):
            keys.append("b:" + str(row["barcode"]).strip())
        if row.get("sku"):
            keys.append("s:" + str(row["sku"]).strip().upper())
        name = normalize_text(row.get("name") or "").replace(" ", "")
        if name:
            keys.append("n:" + name)
        for key in keys:
            if key in first_seen:
                groups.union(index, first_seen[key])
            else:
                first_seen[key] = index
    members: dict[int, list[int]] = defaultdict(list)
    for index in range(len(rows)):
        members[groups.find(index)].append(index)
    return [indexes for indexes in members.values() if len({rows[i]["branch"] for i in indexes}) > 1]


@dataclass
class Transfer:
    item: str
    sku: str
    from_branch: str
    to_branch: str
    qty: int
    unit_value: float
    reason: str          # dead | expiry | overstock
    receiver_urgent: bool
    from_sku: str
    days_left: int | None = None

    @property
    def value(self) -> float:
        return self.qty * self.unit_value


def plan_transfers(rows: list[dict[str, Any]], settings: AnalysisSettings) -> list[Transfer]:
    transfers: list[Transfer] = []
    for indexes in group_items(rows):
        donors = [[i, math.floor(surplus(rows[i], settings))] for i in indexes]
        donors = [donor for donor in donors if donor[1] >= 1]
        receivers = [i for i in indexes if shortage(rows[i], settings) >= 1]
        if not donors or not receivers:
            continue
        # Most urgent receivers first; give away stock that is about to expire or not selling first.
        receivers.sort(key=lambda i: (-num(rows[i].get("stockout_probability_14d")), -num(rows[i].get("avg_daily_sales"))))
        donors.sort(key=lambda d: (
            0 if num(rows[d[0]].get("expiry_risk_qty")) > num(rows[d[0]].get("expired_qty")) else 1 if "dead_stock" in str(rows[d[0]].get("tags")) else 2,
            -d[1],
        ))
        for receiver in receivers:
            need = math.floor(shortage(rows[receiver], settings))
            for donor in donors:
                if need < 1:
                    break
                donor_index, available = donor
                if available < 1 or rows[donor_index]["branch"] == rows[receiver]["branch"]:
                    continue
                source, target = rows[donor_index], rows[receiver]
                qty = min(need, available)
                days_left = source.get("days_left")
                expiring = num(source.get("expiry_risk_qty")) > num(source.get("expired_qty"))
                if expiring and days_left is not None:
                    # Only send what the receiving branch can sell before the expiry date.
                    sellable = math.floor(num(target.get("avg_daily_sales")) * max(int(days_left), 0) - max(num(target.get("stock")), 0))
                    qty = min(qty, max(sellable, 0))
                if qty < 1:
                    continue
                reason = "expiry" if expiring else "dead" if "dead_stock" in str(source.get("tags")) else "overstock"
                transfers.append(
                    Transfer(
                        item=target.get("name") or source.get("name"),
                        sku=target.get("sku"),
                        from_branch=source["branch"],
                        to_branch=target["branch"],
                        qty=int(qty),
                        unit_value=unit_value(source) or unit_value(target),
                        reason=reason,
                        receiver_urgent=num(target.get("stock")) <= 0 or num(target.get("stockout_probability_7d")) >= URGENT_PROBABILITY,
                        from_sku=source.get("sku"),
                        days_left=int(days_left) if expiring and days_left is not None else None,
                    )
                )
                donor[1] -= qty
                need -= qty
    transfers.sort(key=lambda t: (not t.receiver_urgent, -t.value))
    return transfers


@dataclass
class OrderLine:
    sku: str
    barcode: str
    name: str
    stock: float
    daily: float
    qty: int
    unit_cost: float
    priority: str        # urgent | soon | planned
    covered_by_transfer: int
    abc_class: str

    @property
    def value(self) -> float:
        return self.qty * self.unit_cost


@dataclass
class PurchaseOrder:
    branch: str
    supplier: str
    lines: list[OrderLine] = field(default_factory=list)

    @property
    def value(self) -> float:
        return sum(line.value for line in self.lines)

    @property
    def urgent_count(self) -> int:
        return sum(1 for line in self.lines if line.priority == "urgent")


PRIORITY_ORDER = {"urgent": 0, "soon": 1, "planned": 2}


def purchase_orders(rows: list[dict[str, Any]], transfers: list[Transfer]) -> list[PurchaseOrder]:
    incoming: dict[tuple[str, str], int] = defaultdict(int)
    for transfer in transfers:
        incoming[(transfer.to_branch, transfer.sku)] += transfer.qty
    orders: dict[tuple[str, str], PurchaseOrder] = {}
    for row in rows:
        needed = num(row.get("recommended_reorder_qty"))
        if needed <= 0 or num(row.get("avg_daily_sales")) <= 0 or "negative_margin" in str(row.get("tags")):
            continue  # selling below cost: fix the price before buying more
        covered = incoming.get((row["branch"], row.get("sku")), 0)
        qty = math.ceil(needed - covered)
        if qty <= 0:
            continue
        p7, p14 = num(row.get("stockout_probability_7d")), num(row.get("stockout_probability_14d"))
        priority = "urgent" if num(row.get("stock")) <= 0 or p7 >= URGENT_PROBABILITY else "soon" if p14 >= URGENT_PROBABILITY else "planned"
        key = (row["branch"], row.get("supplier") or NO_SUPPLIER)
        order = orders.setdefault(key, PurchaseOrder(branch=key[0], supplier=key[1]))
        order.lines.append(
            OrderLine(
                sku=row.get("sku") or "",
                barcode=row.get("barcode") or "",
                name=row.get("name") or "",
                stock=num(row.get("stock")),
                daily=num(row.get("avg_daily_sales")),
                qty=qty,
                unit_cost=unit_value(row),
                priority=priority,
                covered_by_transfer=covered,
                abc_class=row.get("abc_class") or "",
            )
        )
    for order in orders.values():
        order.lines.sort(key=lambda line: (PRIORITY_ORDER[line.priority], -line.value))
    return sorted(orders.values(), key=lambda order: (order.branch, -order.urgent_count, -order.value))


def stop_buying(rows: list[dict[str, Any]], limit: int = 15) -> list[dict[str, Any]]:
    candidates = [row for row in rows if any(tag in str(row.get("tags")) for tag in ("dead_stock", "overstock_risk"))]
    return sorted(candidates, key=lambda row: -num(row.get("frozen_capital_value")))[:limit]


def price_fixes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((row for row in rows if num(row.get("negative_margin_loss")) > 0), key=lambda row: -num(row.get("negative_margin_loss")))


def expiry_actions(rows: list[dict[str, Any]], transferred: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """Expiring stock that transfers do not already solve: promote it or return it."""
    actions = []
    for row in rows:
        if num(row.get("expiry_risk_value")) <= 0 or (row["branch"], row.get("sku")) in transferred:
            continue
        actions.append(row)
    return sorted(actions, key=lambda row: (num(row.get("days_left")) if row.get("days_left") is not None else 9999, -num(row.get("expiry_risk_value"))))


def abc_matrix(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cells: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"items": 0, "sales": 0.0, "profit": 0.0, "stock_value": 0.0})
    for row in rows:
        abc = row.get("abc_class") or "-"
        xyz = row.get("xyz_class") or "-"
        cell = cells[(abc, xyz)]
        cell["items"] += 1
        cell["sales"] += num(row.get("net_sales"))
        cell["profit"] += num(row.get("gross_profit"))
        cell["stock_value"] += num(row.get("cost_value")) or num(row.get("stock_value"))
    total_sales = sum(cell["sales"] for cell in cells.values()) or 1.0
    xyz_present = sorted({xyz for _abc, xyz in cells}, key=lambda value: "XYZ-".index(value))
    return {
        "rows": [abc for abc in ("A", "B", "C", "-") if any(key[0] == abc for key in cells)],
        "columns": xyz_present,
        "cells": {f"{abc}{xyz}": {**cell, "sales_share": cell["sales"] / total_sales * 100} for (abc, xyz), cell in cells.items()},
    }


def digest_text(branch: str | None, transfers: list[Transfer], orders: list[PurchaseOrder], expiring: list[dict[str, Any]], fixes: list[dict[str, Any]], lang: str = "ar") -> str:
    """A short plain-text summary to paste into WhatsApp or email."""
    ar = lang == "ar"
    lines = [("ملخص النهارده" if ar else "Today's summary") + (f" - {branch}" if branch else "")]
    if transfers:
        value = sum(t.value for t in transfers)
        lines.append((f"🔁 {len(transfers)} تحويل بين الفروع بقيمة {value:,.0f} بدل الشراء" if ar else f"🔁 {len(transfers)} branch transfers worth {value:,.0f} instead of buying"))
        for transfer in transfers[:5]:
            lines.append(f"  - {transfer.item}: {transfer.qty} {'من' if ar else 'from'} {transfer.from_branch} {'إلى' if ar else 'to'} {transfer.to_branch}")
    urgent = [line for order in orders for line in order.lines if line.priority == "urgent"]
    if orders:
        total = sum(order.value for order in orders)
        lines.append((f"🛒 شراء بقيمة {total:,.0f} من {len({o.supplier for o in orders})} مورد، منهم {len(urgent)} صنف عاجل" if ar
                      else f"🛒 Purchases worth {total:,.0f} from {len({o.supplier for o in orders})} suppliers, {len(urgent)} urgent items"))
        for line in urgent[:5]:
            lines.append(f"  - {line.name}: {line.qty}")
    if expiring:
        lines.append((f"⏳ {len(expiring)} صنف صلاحيته قربت ومحتاج عرض أو مرتجع" if ar else f"⏳ {len(expiring)} items near expiry need a promotion or return"))
    if fixes:
        lines.append((f"⚠️ {len(fixes)} صنف بيتباع بأقل من التكلفة: راجع السعر" if ar else f"⚠️ {len(fixes)} items sell below cost: check the price"))
    return "\n".join(lines)


def whatsapp_link(text: str) -> str:
    return "https://wa.me/?text=" + quote(text)
