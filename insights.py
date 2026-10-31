"""Explain where the profit comes from and where the money leaks.

Works on analyzed item rows (the merged_items.csv schema) and returns plain data that the
web page and summary.json both use. Every amount is traceable to the items behind it.

Money is split into three kinds so they are never added together by mistake:
- loss:   money already lost or very likely to be lost (selling below cost, expiry, missed profit)
- frozen: cash sitting in stock that does not move (it is recoverable, not lost yet)
- info:   patterns worth knowing (thin margins on best sellers, profit concentration)
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

TOP_ITEMS = 8
PARETO_SHARE = 0.8
THIN_MARGIN_PERCENT = 8.0


def num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def item_ref(item: dict[str, Any], amount: float, detail_ar: str = "", detail_en: str = "") -> dict[str, Any]:
    return {
        "name": item.get("name") or item.get("ref"),
        "ref": item.get("ref"),
        "branch": item.get("branch"),
        "amount": round(amount, 2),
        "detail_ar": detail_ar,
        "detail_en": detail_en,
    }


def top(items: list[dict[str, Any]], key, limit: int = TOP_ITEMS) -> list[dict[str, Any]]:
    return sorted((item for item in items if key(item) > 0), key=key, reverse=True)[:limit]


def fmt(value: float) -> str:
    return f"{value:,.0f}"


def analyze_profit_loss(items: list[dict[str, Any]], period_days: int, coverage: dict[str, bool]) -> dict[str, Any]:
    has_cost = bool(coverage.get("cost"))
    has_sales = bool(coverage.get("sales"))
    has_stock = bool(coverage.get("stock"))
    span_ar = f"آخر {period_days} يوم" if period_days else "الفترة المرفوعة"
    span_en = f"the last {period_days} days" if period_days else "the uploaded period"

    revenue = sum(num(i.get("net_sales")) for i in items)
    costed = [i for i in items if num(i.get("cost_known")) and num(i.get("pos_sales")) > 0]
    cogs = sum(num(i.get("pos_sales")) * num(i.get("cost")) for i in costed)
    costed_revenue = sum(num(i.get("net_sales")) for i in costed)
    gross_profit = sum(num(i.get("gross_profit")) for i in costed)
    margin_pct = (gross_profit / costed_revenue * 100) if costed_revenue > 0 else 0.0
    cost_coverage_pct = (costed_revenue / revenue * 100) if revenue > 0 else 0.0

    # --- where the profit comes from -------------------------------------------------
    driver_value = (lambda i: num(i.get("gross_profit"))) if has_cost else (lambda i: num(i.get("net_sales")))
    ranked = sorted((i for i in items if driver_value(i) > 0), key=driver_value, reverse=True)
    positive_total = sum(driver_value(i) for i in ranked)
    running, pareto_count = 0.0, 0
    for item in ranked:
        if positive_total <= 0 or running >= positive_total * PARETO_SHARE:
            break
        running += driver_value(item)
        pareto_count += 1
    selling_items = sum(1 for i in items if num(i.get("pos_sales")) > 0)
    categories: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for item in items:
        bucket = categories[item.get("category") or "UNCATEGORIZED"]
        bucket["revenue"] += num(item.get("net_sales"))
        bucket["profit"] += num(item.get("gross_profit"))
        bucket["frozen"] += num(item.get("frozen_capital_value"))
        bucket["items"] += 1
    category_rows = [
        {
            "category": name,
            "revenue": round(values["revenue"], 2),
            "profit": round(values["profit"], 2),
            "margin_pct": round(values["profit"] / values["revenue"] * 100, 1) if values["revenue"] > 0 and has_cost else None,
            "frozen": round(values["frozen"], 2),
            "items": int(values["items"]),
        }
        for name, values in categories.items()
    ]
    category_rows.sort(key=lambda row: row["profit"] if has_cost else row["revenue"], reverse=True)

    profit_sources = {
        "basis": "profit" if has_cost else "revenue",
        "pareto_count": pareto_count,
        "selling_items": selling_items,
        "pareto_share_of_items": round(pareto_count / selling_items * 100, 1) if selling_items else 0.0,
        "top_items": [
            {
                **item_ref(i, driver_value(i)),
                "share": round(driver_value(i) / positive_total * 100, 1) if positive_total else 0.0,
                "margin_pct": round(num(i.get("gross_margin_percent")), 1) if has_cost else None,
                "qty": num(i.get("pos_sales")),
            }
            for i in ranked[:10]
        ],
        "categories": category_rows[:12],
    }

    # --- where the money leaks -------------------------------------------------------
    drivers: list[dict[str, Any]] = []

    def add_driver(key: str, kind: str, rows: list[dict[str, Any]], amount_of, title: tuple[str, str], why: tuple[str, str], action: tuple[str, str], detail=None) -> None:
        amount = sum(amount_of(i) for i in rows)
        if amount <= 0:
            return
        detail = detail or (lambda i: ("", ""))
        drivers.append({
            "key": key,
            "kind": kind,
            "amount": round(amount, 2),
            "items_count": sum(1 for i in rows if amount_of(i) > 0),
            "title_ar": title[0], "title_en": title[1],
            "why_ar": why[0], "why_en": why[1],
            "action_ar": action[0], "action_en": action[1],
            "top_items": [item_ref(i, amount_of(i), *detail(i)) for i in top(rows, amount_of)],
        })

    if has_cost and has_sales:
        add_driver(
            "negative_margin", "loss", items, lambda i: num(i.get("negative_margin_loss")),
            ("بيع بأقل من التكلفة", "Selling below cost"),
            ("أصناف سعر بيعها الفعلي أقل من تكلفتها، فكل وحدة بتتباع بتخسرك فلوس. غالباً خطأ تسعير أو تكلفة متسجلة غلط.",
             "Items whose actual selling price is below cost, so every unit sold loses money. Usually a pricing or cost-entry error."),
            ("راجع سعر البيع والتكلفة المسجلة للأصناف دي النهارده.", "Review the selling price and recorded cost of these items today."),
            lambda i: (f"التكلفة {num(i.get('cost')):,.2f} / البيع {num(i.get('sales_price')):,.2f}", f"cost {num(i.get('cost')):,.2f} / price {num(i.get('sales_price')):,.2f}"),
        )
    if has_stock:
        add_driver(
            "expiry", "loss", items, lambda i: num(i.get("expiry_risk_value")),
            ("صلاحية منتهية أو هتنتهي قبل البيع", "Expired or expiring before it sells"),
            ("كميات انتهت صلاحيتها أو هتنتهي خلال 6 شهور قبل ما تتباع بمعدل البيع الحالي (حساب بالتشغيلة، الأقدم يتباع الأول).",
             "Units already expired, or that will expire within 6 months before they sell at the current rate (batch-level, first-expiry-first-out)."),
            ("اعمل عرض أو تحويل لفرع بيبيع الصنف، أو رجّعه للمورد قبل ميعاد المرتجع.", "Promote, transfer to a branch that sells it, or return to the supplier before the deadline."),
            lambda i: (f"{num(i.get('expiry_risk_qty')):,.0f} وحدة، الصلاحية {i.get('expiry') or '-'}", f"{num(i.get('expiry_risk_qty')):,.0f} units, expiry {i.get('expiry') or '-'}"),
        )
    if has_stock and has_sales:
        lost_key = "lost_profit_value" if has_cost else "lost_sales_value"
        add_driver(
            "stockout", "loss", items, lambda i: num(i.get(lost_key)),
            ("ربح ضايع بسبب النواقص" if has_cost else "مبيعات ضايعة بسبب النواقص", "Profit lost to stockouts" if has_cost else "Sales lost to stockouts"),
            ("أصناف عليها طلب والرصيد مش هيكفي الـ30 يوم الجايين، فالزبون هيشتري من مكان تاني. الرقم هو الربح المتوقع ضياعه خلال 30 يوم.",
             "Items in demand whose stock will not cover the next 30 days, so customers buy elsewhere. Amount is the expected missed margin over 30 days."),
            ("اطلب الكمية المقترحة أو حوّل من فرع عنده زيادة.", "Order the suggested quantity or transfer from a branch with excess."),
            lambda i: (f"الرصيد {num(i.get('qoh')):,.0f}، اقتراح شراء {num(i.get('recommended_reorder_qty')):,.0f}", f"stock {num(i.get('qoh')):,.0f}, reorder {num(i.get('recommended_reorder_qty')):,.0f}"),
        )
        dead_value = (lambda i: num(i.get("cost_value")) if "dead_stock" in str(i.get("tags")) else 0.0) if has_cost else (lambda i: num(i.get("stock_value")) if "dead_stock" in str(i.get("tags")) else 0.0)
        add_driver(
            "dead_stock", "frozen", items, dead_value,
            ("فلوس نايمة في مخزون راكد", "Cash stuck in dead stock"),
            (f"أصناف ليها رصيد ومتباعش منها ولا وحدة في {span_ar}. الفلوس دي مش خسارة لسه، لكنها واقفة ومعرضة لانتهاء الصلاحية.",
             f"Items with stock and zero sales in {span_en}. Not lost yet, but idle and exposed to expiry."),
            ("حوّلها لفرع بيبيعها، اعمل باكدج مع صنف سريع، أو رجّعها للمورد.", "Transfer to a branch that sells them, bundle with a fast seller, or return to supplier."),
            lambda i: (f"رصيد {num(i.get('qoh')):,.0f} بدون بيع", f"{num(i.get('qoh')):,.0f} units, no sales"),
        )
        add_driver(
            "overstock", "frozen", items, lambda i: num(i.get("excess_value")),
            ("زيادة عن احتياج 3 شهور", "Stock beyond 3 months of need"),
            ("أصناف بتتباع، لكن الرصيد أكبر من احتياج 90 يوم. الزيادة دي فلوس كان ممكن تشتري بيها أصناف ناقصة.",
             "Items that sell, but stock exceeds 90 days of demand. The excess is cash that could fund items you are short of."),
            ("وقّف الشراء لحد ما الرصيد ينزل، أو حوّل الزيادة.", "Stop buying until stock normalises, or transfer the excess."),
            lambda i: (f"زيادة {num(i.get('excess_qty')):,.0f} وحدة، تغطية {num(i.get('days_cover')):,.0f} يوم", f"{num(i.get('excess_qty')):,.0f} excess units, {num(i.get('days_cover')):,.0f} days cover"),
        )
    if has_cost and has_sales and ranked:
        volume_cut = sorted((num(i.get("net_sales")) for i in costed), reverse=True)
        threshold = volume_cut[max(len(volume_cut) // 5 - 1, 0)] if volume_cut else 0
        thin = [i for i in costed if num(i.get("net_sales")) >= threshold and 0 < num(i.get("gross_margin_percent")) < THIN_MARGIN_PERCENT]
        add_driver(
            "thin_margin", "info", thin, lambda i: num(i.get("net_sales")),
            ("أصناف بتبيع كتير بهامش ضعيف", "Best sellers with thin margins"),
            (f"من أعلى 20% مبيعات، لكن هامش ربحها أقل من {THIN_MARGIN_PERCENT:.0f}%. حجم شغل كبير بربح قليل.",
             f"Among your top 20% sellers, but with margin under {THIN_MARGIN_PERCENT:.0f}%. Lots of work for little profit."),
            ("فاوض المورد على خصم أو بونص، أو راجع السعر.", "Negotiate supplier discount or bonus, or review the price."),
            lambda i: (f"هامش {num(i.get('gross_margin_percent')):.1f}%", f"margin {num(i.get('gross_margin_percent')):.1f}%"),
        )

    drivers.sort(key=lambda d: ({"loss": 0, "frozen": 1, "info": 2}[d["kind"]], -d["amount"]))
    total_loss = sum(d["amount"] for d in drivers if d["kind"] == "loss")
    total_frozen = sum(d["amount"] for d in drivers if d["kind"] == "frozen")
    for driver in drivers:
        base = total_loss if driver["kind"] == "loss" else total_frozen if driver["kind"] == "frozen" else 0
        driver["share"] = round(driver["amount"] / base * 100, 1) if base else None

    # --- the story in plain words ----------------------------------------------------
    headline_ar: list[str] = []
    headline_en: list[str] = []
    if has_cost and costed_revenue > 0:
        headline_ar.append(f"ربحك الإجمالي في {span_ar}: {fmt(gross_profit)} من مبيعات {fmt(costed_revenue)} (هامش {margin_pct:.1f}%).")
        headline_en.append(f"Gross profit over {span_en}: {fmt(gross_profit)} on {fmt(costed_revenue)} sales ({margin_pct:.1f}% margin).")
    elif has_sales:
        headline_ar.append(f"مبيعاتك في {span_ar}: {fmt(revenue)}. مفيش تكلفة في الملفات، فالربح الحقيقي مش ممكن يتحسب.")
        headline_en.append(f"Sales over {span_en}: {fmt(revenue)}. No cost data was provided, so true profit cannot be computed.")
    if pareto_count and selling_items:
        basis_ar = "ربحك" if has_cost else "مبيعاتك"
        basis_en = "profit" if has_cost else "sales"
        headline_ar.append(f"80% من {basis_ar} جاية من {pareto_count} صنف بس من أصل {selling_items} صنف بيتباع. دول لازم ميخلصوش أبداً.")
        headline_en.append(f"80% of your {basis_en} comes from just {pareto_count} of {selling_items} selling items. Never let these run out.")
    losses = [d for d in drivers if d["kind"] == "loss"]
    if losses:
        first = losses[0]
        headline_ar.append(f"أكبر سبب للخسارة: {first['title_ar']} بقيمة {fmt(first['amount'])} ({first['share']:.0f}% من إجمالي الخسائر {fmt(total_loss)}).")
        headline_en.append(f"Biggest loss driver: {first['title_en'].lower()} worth {fmt(first['amount'])} ({first['share']:.0f}% of {fmt(total_loss)} total losses).")
    if total_frozen > 0:
        headline_ar.append(f"عندك {fmt(total_frozen)} متجمدة في مخزون مش بيتحرك. لو اتحررت تقدر تمول بيها الأصناف اللي بتجيب الربح.")
        headline_en.append(f"{fmt(total_frozen)} is frozen in stock that does not move. Freeing it funds the items that make your profit.")
    if has_cost and gross_profit > 0 and total_loss > 0:
        headline_ar.append(f"الخسائر دي تساوي {total_loss / gross_profit * 100:.0f}% من ربحك. يعني تقليلها هو أسرع طريق لزيادة المكسب.")
        headline_en.append(f"These losses equal {total_loss / gross_profit * 100:.0f}% of your profit, so cutting them is the fastest way to earn more.")

    warnings = []
    if not has_cost:
        warnings.append("no_cost")
    elif cost_coverage_pct < 90:
        warnings.append("partial_cost")
    if not has_stock:
        warnings.append("no_stock")
    if not has_sales:
        warnings.append("no_sales")

    return {
        "period_days": period_days,
        "coverage": coverage,
        "revenue": round(revenue, 2),
        "cogs": round(cogs, 2),
        "gross_profit": round(gross_profit, 2),
        "margin_pct": round(margin_pct, 2),
        "cost_coverage_pct": round(cost_coverage_pct, 1),
        "total_loss": round(total_loss, 2),
        "total_frozen": round(total_frozen, 2),
        "profit_sources": profit_sources,
        "drivers": drivers,
        "headline_ar": headline_ar,
        "headline_en": headline_en,
        "warnings": warnings,
    }
