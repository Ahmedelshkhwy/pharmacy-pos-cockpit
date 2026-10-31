"""Daily demand estimate from invoice lines.

A plain "units sold / days in period" undercounts demand whenever the item could not be
sold: it was out of stock, or it was not on the shelf yet. This module estimates those
days from the sales pattern itself and weights recent days more than old ones.

Both choices were picked by backtesting on ~1M real invoice lines (tools/backtest_forecast.py).
A month-of-year seasonal factor was tried too and made forecasts worse with one year of
history, so it is not used.

Everything here works on day indexes (0 = first day of the period) so it stays fast on
a million invoice lines.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

GAP_PROBABILITY = 0.01           # a zero-sales gap this unlikely means the item was unavailable
GAP_THRESHOLD = -math.log(GAP_PROBABILITY)
MIN_AVAILABLE_DAYS = 7
MIN_AVAILABLE_SHARE = 0.2
TREND_WINDOW_DAYS = 30
TREND_MIN_UNITS = 10
TREND_UP, TREND_DOWN = 1.25, 0.8
DEFAULT_HALF_LIFE_DAYS = 30


@dataclass
class Forecast:
    rate: float                 # units/day used for decisions
    base_rate: float            # plain units / period days
    available_days: int
    unavailable_days: int
    trend: str = ""             # "up" | "down" | "flat" | ""
    trend_ratio: float = 1.0


class DayWeights:
    """Recency weights with prefix sums, so the weight of any day range is O(1)."""

    def __init__(self, start: date, days: int, half_life: float) -> None:
        self.start, self.days = start, days
        self.weights = [0.5 ** ((days - 1 - index) / half_life) if half_life > 0 else 1.0 for index in range(days)]
        self.prefix = [0.0]
        for weight in self.weights:
            self.prefix.append(self.prefix[-1] + weight)

    def range_sum(self, first: int, last: int) -> float:
        return self.prefix[last + 1] - self.prefix[first] if last >= first else 0.0


def unavailable_ranges(sale_days: list[int], days: int, total_qty: float, qoh: float | None) -> list[tuple[int, int]]:
    """Zero-sales stretches that are too long to be chance at the item's own selling rate.

    - before the first sale: not listed yet or out of stock
    - between sales: out of stock
    - after the last sale: only when current stock is zero (it ran out); with stock on the
      shelf, no sales means no demand, and with unknown stock we cannot tell, so we keep it.
    """
    if not sale_days or total_qty <= 0:
        return []
    ranges: list[tuple[int, int]] = []
    # Model how often the item sells (share of days with a sale), not how many units:
    # demand often comes as a few large orders, and unit rates would flag normal quiet spells.
    selling_share = len(sale_days) / days
    for _ in range(2):  # re-estimate once on the days the item was really available
        ranges = []
        if selling_share >= 1:
            break
        log_quiet = math.log(1 - selling_share)  # log P(no sale on a given day)
        previous = -1
        for day in sale_days:
            gap = day - previous - 1
            if gap > 0 and -log_quiet * gap > GAP_THRESHOLD:
                ranges.append((previous + 1, day - 1))
            previous = day
        tail = days - 1 - sale_days[-1]
        if tail > 0 and qoh is not None and qoh <= 0:
            ranges.append((sale_days[-1] + 1, days - 1))
        available = days - sum(last - first + 1 for first, last in ranges)
        selling_share = min(len(sale_days) / max(available, 1), 0.999)
    return ranges


def forecast_item(
    daily: dict[int, float],
    weights: DayWeights,
    qoh: float | None,
    correct_stockouts: bool = True,
) -> Forecast:
    days = weights.days
    total = sum(daily.values())
    base_rate = max(total, 0.0) / days if days else 0.0
    if total <= 0:
        return Forecast(rate=0.0, base_rate=base_rate, available_days=days, unavailable_days=0)

    sale_days = sorted(day for day, qty in daily.items() if qty > 0)
    ranges = unavailable_ranges(sale_days, days, total, qoh) if correct_stockouts else []
    unavailable = sum(last - first + 1 for first, last in ranges)
    floor_days = max(MIN_AVAILABLE_DAYS, math.ceil(days * MIN_AVAILABLE_SHARE))
    if days - unavailable < floor_days:
        # Too little evidence: keep the correction modest instead of extrapolating a few days.
        ranges, unavailable = [], max(days - floor_days, 0)
    available = days - unavailable

    weight_total = weights.range_sum(0, days - 1) - sum(weights.range_sum(a, b) for a, b in ranges)
    weighted_qty = sum(weights.weights[day] * qty for day, qty in daily.items() if 0 <= day < days)
    if ranges or not unavailable:
        rate = weighted_qty / weight_total if weight_total > 0 else 0.0
    else:
        rate = total / available  # floor case: plain corrected average

    trend, trend_ratio = "", 1.0
    corrected_rate = total / available if available else 0.0
    if total >= TREND_MIN_UNITS and corrected_rate > 0 and days > TREND_WINDOW_DAYS:
        recent_first = days - TREND_WINDOW_DAYS
        recent_qty = sum(qty for day, qty in daily.items() if day >= recent_first)
        recent_unavailable = sum(max(0, last - max(first, recent_first) + 1) for first, last in ranges if last >= recent_first)
        recent_days = TREND_WINDOW_DAYS - recent_unavailable
        if recent_days >= MIN_AVAILABLE_DAYS:
            trend_ratio = (recent_qty / recent_days) / corrected_rate
            trend = "up" if trend_ratio >= TREND_UP else "down" if trend_ratio <= TREND_DOWN else "flat"

    return Forecast(
        rate=max(rate, 0.0),
        base_rate=base_rate,
        available_days=available,
        unavailable_days=unavailable,
        trend=trend,
        trend_ratio=round(trend_ratio, 3),
    )


def to_day_index(daily: dict[date, float], start: date) -> dict[int, float]:
    return {(day - start).days: qty for day, qty in daily.items()}
