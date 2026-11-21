"""Daily transfer trip: the solver checked against brute force, and the rules of a valid tour."""
from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import trip_plan  # noqa: E402
from actions import Transfer  # noqa: E402
from settings import AnalysisSettings  # noqa: E402
from transport import DistanceMatrix  # noqa: E402

# Four branches on a rough line, ~10 km apart (0.09 degrees of latitude ~ 10 km).
PLACES = {"Base": (30.00, 31.20), "A": (30.09, 31.20), "B": (30.18, 31.20), "C": (30.27, 31.20)}


def transfer(source, target, qty=5, cost=10.0, urgent=True, reason="overstock", sku=None):
    sku = sku or f"{source}-{target}-{qty}"
    return Transfer(item=sku, sku=sku, from_branch=source, to_branch=target, qty=qty, unit_value=cost,
                    reason=reason, receiver_urgent=urgent, from_sku=sku)


def rows_for(transfers, daily=2.0, stock=0.0, price=15.0):
    return [{"branch": t.to_branch, "sku": t.sku, "price": price, "avg_daily_sales": daily, "stock": stock, "lead_time_days": 7}
            for t in transfers]


def brute_force(base, candidates, matrix, settings):
    """Best value - km cost over every subset of transfers and every valid order of stops."""
    best = 0.0
    branches = sorted({b for c in candidates for b in (c.transfer.from_branch, c.transfer.to_branch)} - {base})
    for size in range(len(branches) + 1):
        for visited in itertools.permutations(branches, size):
            tour = [base, *visited, base]
            km = sum(matrix.leg(a, b).km for a, b in zip(tour, tour[1:]))
            minutes = sum(matrix.leg(a, b).minutes for a, b in zip(tour, tour[1:])) + settings.stop_minutes * size
            if minutes > settings.shift_hours * 60 + 1e-6:
                continue
            position = {b: i + 1 for i, b in enumerate(visited)}
            position[base] = None
            value = 0.0
            for c in candidates:
                s, t = c.transfer.from_branch, c.transfer.to_branch
                ok_s = s == base or s in position and position[s] is not None
                ok_t = t == base or t in position and position[t] is not None
                if ok_s and ok_t and (s == base or t == base or position[s] < position[t]):
                    value += c.value
            best = max(best, value - (settings.cost_per_km + trip_plan.TIE_BREAK_PER_KM) * km)
    return best


def objective(plan, settings):
    return sum(c.value for c in plan.carried) - (settings.cost_per_km + trip_plan.TIE_BREAK_PER_KM) * plan.km


def assert_valid(plan, settings):
    order = [plan.base] + [s.branch for s in plan.stops] + [plan.base]
    assert plan.minutes <= settings.shift_hours * 60 + 1.0   # the solver rounds minutes per leg
    for c in plan.carried:
        assert c.pickup_at < c.drop_at, "pickup before delivery"
        assert order[c.pickup_at + 1] == c.transfer.from_branch and order[c.drop_at + 1] == c.transfer.to_branch


@pytest.mark.parametrize("seed", range(20))
def test_solver_is_never_worse_than_the_best_single_visit_tour(seed):
    rng = random.Random(seed)
    names = ["Base", "A", "B", "C"]
    places = {n: (30 + rng.uniform(-0.15, 0.15), 31.2 + rng.uniform(-0.15, 0.15)) for n in names}
    matrix = DistanceMatrix(places, road_factor=1.3, speed_kmh=30)
    transfers = []
    for _ in range(rng.randint(2, 6)):
        a, b = rng.sample(names, 2)
        transfers.append(transfer(a, b, qty=rng.randint(1, 8), cost=rng.choice([5, 20, 60]), urgent=rng.random() < 0.6,
                                  sku=f"s{len(transfers)}"))
    settings = AnalysisSettings(cost_per_km=rng.choice([0, 1, 5, 20]), shift_hours=rng.choice([1, 2, 6]), stop_minutes=10)
    rows = rows_for(transfers, daily=rng.uniform(0.2, 3))
    plan = trip_plan.plan_trip(transfers, rows, settings, matrix, "Base", stale=set())
    assert_valid(plan, settings)
    candidates = trip_plan.transfer_values(transfers, rows, settings)
    # The routing solver may also visit a branch twice, so it can only match or beat the best tour
    # that visits each branch once (found here by trying every subset and order).
    assert objective(plan, settings) >= brute_force("Base", candidates, matrix, settings) - 0.05


def test_a_cheap_transfer_rides_along_but_does_not_justify_a_trip():
    matrix = DistanceMatrix(PLACES, road_factor=1.0, speed_kmh=60)
    settings = AnalysisSettings(cost_per_km=10, release_value_percent=0, stop_minutes=5)
    big = transfer("A", "B", qty=40, cost=50, urgent=True, sku="big")         # worth a trip on its own
    small = transfer("A", "B", qty=1, cost=2, urgent=False, sku="small")      # rides along: same stops
    far = transfer("C", "Base", qty=1, cost=2, urgent=False, sku="far")       # alone it would need C
    rows = rows_for([big, small, far], daily=5, price=80)
    plan = trip_plan.plan_trip([big, small, far], rows, settings, matrix, "Base", stale=set())
    carried = {c.transfer.sku for c in plan.carried}
    assert carried == {"big", "small"}
    assert [s.branch for s in plan.stops] == ["A", "B"]
    far_candidate = next(c for c in plan.candidates if c.transfer.sku == "far")
    assert far_candidate.reason in ("wait", "not_worth") and far_candidate.detour_km > 0
    assert plan.separate_trips == 2 and plan.separate_km > plan.km


def test_shift_limit_and_reasons():
    matrix = DistanceMatrix(PLACES, road_factor=1.0, speed_kmh=30)
    settings = AnalysisSettings(cost_per_km=0, shift_hours=1, stop_minutes=10)
    near = transfer("A", "Base", qty=10, sku="near")
    distant = transfer("C", "Base", qty=10, sku="distant")     # 30 km away: 120 min round trip
    rows = rows_for([near, distant], daily=4)
    plan = trip_plan.plan_trip([near, distant], rows, settings, matrix, "Base", stale=set())
    assert {c.transfer.sku for c in plan.carried} == {"near"}
    assert next(c for c in plan.candidates if c.transfer.sku == "distant").reason == "no_time"
    assert plan.all_km is not None and plan.all_km > plan.km   # carrying everything needs the longer tour


def test_stale_and_unlocated_branches_are_left_out():
    places = {k: v for k, v in PLACES.items() if k != "C"}
    matrix = DistanceMatrix(places)
    moves = [transfer("A", "B", sku="ok"), transfer("B", "C", sku="no-place"), transfer("A", "Base", sku="stale")]
    plan = trip_plan.plan_trip(moves, rows_for(moves), AnalysisSettings(), matrix, "Base", stale={"Base"})
    reasons = {c.transfer.sku: c.reason for c in plan.candidates}
    assert reasons["no-place"] == "no_location" and reasons["stale"] == "stale"
    assert plan.excluded == {"C": "no_location", "Base": "stale"}


def test_no_base_and_nothing_to_carry():
    matrix = DistanceMatrix(PLACES)
    assert trip_plan.plan_trip([transfer("A", "B")], rows_for([transfer("A", "B")]), AnalysisSettings(), matrix, "", set()).status == "no_base"
    assert trip_plan.plan_trip([], [], AnalysisSettings(), matrix, "Base", set()).status == "nothing"


def test_both_directions_ride_on_one_day():
    # One item goes A to B and another B to A: visiting A, B, then A again carries both.
    matrix = DistanceMatrix(PLACES, road_factor=1.0, speed_kmh=60)
    settings = AnalysisSettings(cost_per_km=1, stop_minutes=5)
    there, back = transfer("A", "B", qty=20, sku="there"), transfer("B", "A", qty=20, sku="back")
    plan = trip_plan.plan_trip([there, back], rows_for([there, back], daily=5, price=80), settings, matrix, "Base", set())
    assert {c.transfer.sku for c in plan.carried} == {"there", "back"}
    assert_valid(plan, settings)


def test_loading_at_the_base_is_listed_apart():
    matrix = DistanceMatrix(PLACES, road_factor=1.0, speed_kmh=60)
    out, home = transfer("Base", "A", qty=20, sku="out"), transfer("B", "Base", qty=20, sku="home")
    plan = trip_plan.plan_trip([out, home], rows_for([out, home], daily=5, price=80), AnalysisSettings(cost_per_km=1), matrix, "Base", set())
    assert [c.transfer.sku for c in plan.load_at_base] == ["out"]
    assert [c.transfer.sku for c in plan.unload_at_base] == ["home"]


def test_detour_counts_pickup_before_delivery():
    matrix = DistanceMatrix(PLACES, road_factor=1.0)
    tour = ["Base", "B", "Base"]
    # A is on the way to B: picking up at A then delivering at B adds almost nothing.
    assert trip_plan.cheapest_detour(tour, "A", "B", matrix) == pytest.approx(0.0, abs=0.5)
    # On the line Base - A - B - C: delivering to C after A means going 20 km on and coming back.
    assert trip_plan.cheapest_detour(["Base", "A", "Base"], "A", "C", matrix) == pytest.approx(40.0, abs=1.5)
    # Picking up at B and delivering to the base adds only the trip to B.
    assert trip_plan.cheapest_detour(["Base", "Base"], "B", "Base", matrix) == pytest.approx(40.0, abs=1.5)


def test_same_data_gives_the_same_plan_on_a_large_chain():
    rng = random.Random(11)
    names = ["Base"] + [f"B{i}" for i in range(14)]
    matrix = DistanceMatrix({n: (30 + rng.uniform(-0.2, 0.2), 31.2 + rng.uniform(-0.25, 0.25)) for n in names})
    moves = []
    for i in range(50):
        a, b = rng.sample(names, 2)
        moves.append(transfer(a, b, qty=rng.randint(1, 20), cost=rng.choice([5, 20, 60]), urgent=rng.random() < 0.4, sku=f"s{i}"))
    rows = rows_for(moves, daily=1.5, price=70)
    settings = AnalysisSettings(cost_per_km=3, shift_hours=6)
    first = trip_plan.plan_trip(moves, rows, settings, matrix, "Base", set())
    second = trip_plan.plan_trip(moves, rows, settings, matrix, "Base", set())
    assert first.method == "search" and first.stops
    assert [s.branch for s in first.stops] == [s.branch for s in second.stops]
    assert first.value == second.value and first.km == second.km
    assert_valid(first, settings)
    # Carrying everything would take a much longer tour than the chosen one.
    assert first.all_km > first.km
