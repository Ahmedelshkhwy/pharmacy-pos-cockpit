"""Daily transfer trip: which transfers to carry today and the order of the stops.

One vehicle leaves the base branch, visits some branches and comes back. A transfer is worth
carrying when what it prevents is worth more than the extra driving it adds:

    value = expected loss prevented at the receiver until a supplier order could arrive
            (the same unit value as the budget plan: P(the unit sells) x (profit + customer loss))
          + stock about to expire at the sender that the receiver will sell
          + a small share of the money moved out of idle stock (release_value_percent)

The choice is a pickup-and-delivery tour with optional stops:
    maximize   sum value * carried  -  cost_per_km * km driven
    subject to one tour from the base and back, pickup before delivery,
               driving time + time at stops <= the driver's shift
Small chains are solved exactly with HiGHS; larger ones with an iterated local search that runs a
fixed number of rounds, so the same data always gives the same plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from actions import Transfer, num
from budget_plan import sell_probabilities
from settings import AnalysisSettings
from transport import DistanceMatrix

EXACT_MAX_BRANCHES = 9       # up to this many branches the best single-visit tour is proven with HiGHS
SEARCH_ROUNDS = 40           # rounds of the local search on larger chains (fixed, so results repeat)
TIE_BREAK_PER_KM = 1e-3      # with no cost per km, still prefer the shorter of two equal tours


@dataclass
class Candidate:
    transfer: Transfer
    value: float
    urgent_value: float
    expiry_value: float
    release_value: float
    carried: bool = False
    reason: str = ""             # why it is not carried: wait | not_worth | no_time | stale | no_location
    detour_km: float = 0.0       # extra kilometres it would add to today's trip
    pickup_at: int = -2          # stop number where it is loaded (-1: at the base before leaving)
    drop_at: int = -2            # stop number where it is unloaded (len(stops): at the base on return)


@dataclass
class Stop:
    branch: str
    arrive_minute: float
    pickups: list[Candidate] = field(default_factory=list)
    drops: list[Candidate] = field(default_factory=list)


@dataclass
class TripPlan:
    base: str
    stops: list[Stop]
    km: float
    minutes: float
    value: float
    cost: float
    candidates: list[Candidate]
    excluded: dict[str, str]           # branch -> stale | no_location
    all_km: float | None               # one tour carrying every transfer
    all_minutes: float | None
    separate_km: float                 # every branch pair its own round trip from the base
    separate_trips: int
    status: str                        # solved | nothing | no_base
    method: str = ""                    # exact | exact+ | search
    load_at_base: list[Candidate] = field(default_factory=list)      # loaded before leaving
    unload_at_base: list[Candidate] = field(default_factory=list)    # unloaded on return

    @property
    def carried(self) -> list[Candidate]:
        return [c for c in self.candidates if c.carried]

    @property
    def left_out(self) -> list[Candidate]:
        return [c for c in self.candidates if not c.carried]


def transfer_values(transfers: list[Transfer], rows: list[dict[str, Any]], settings: AnalysisSettings) -> list[Candidate]:
    by_key = {(row["branch"], row.get("sku") or ""): row for row in rows}
    candidates = []
    for transfer in transfers:
        receiver = by_key.get((transfer.to_branch, transfer.sku), {})
        price = num(receiver.get("price"))
        cost = transfer.unit_value
        profit = max(price - cost, 0.0) if price > 0 else 0.0
        unit_loss = profit + settings.customer_loss_percent / 100 * (price if price > 0 else cost)
        # Without the transfer the receiver can still order from the supplier, which takes the lead time.
        lead = num(receiver.get("lead_time_days")) or settings.lead_time_days
        probabilities = sell_probabilities(num(receiver.get("avg_daily_sales")) * lead, max(num(receiver.get("stock")), 0.0), transfer.qty)
        urgent = sum(probabilities) * unit_loss
        expiry = transfer.qty * cost if transfer.reason == "expiry" else 0.0
        release = transfer.qty * cost * settings.release_value_percent / 100
        candidates.append(Candidate(transfer, urgent + expiry + release, urgent, expiry, release))
    return candidates


def _tour_cost(order: list[str], matrix: DistanceMatrix, stop_minutes: float) -> tuple[float, float]:
    """Kilometres and minutes of a tour; passing back through the base costs no stop time."""
    km = sum(matrix.leg(a, b).km for a, b in zip(order, order[1:]))
    stops = sum(1 for branch in order[1:-1] if branch != order[0])
    minutes = sum(matrix.leg(a, b).minutes for a, b in zip(order, order[1:])) + stop_minutes * stops
    return km, minutes


def _single_visit_plan(base: str, pairs: list[tuple[str, str]], pair_value: dict[tuple[str, str], float],
                       matrix: DistanceMatrix, settings: AnalysisSettings, time_limit: float) -> tuple[list[str], set[tuple[str, str]]]:
    """Best tour that visits each branch at most once (HiGHS). Exact on small chains; on large ones it
    stops at the time limit and serves as the starting plan for the routing solver."""
    import highspy

    branches = sorted({b for pair in pairs for b in pair} - {base})
    nodes = ["<start>", *branches, "<end>"]
    location = {"<start>": base, "<end>": base, **{b: b for b in branches}}
    n = len(branches)
    model = highspy.Highs()
    model.setOptionValue("output_flag", False)
    model.setOptionValue("time_limit", time_limit)
    model.setOptionValue("mip_rel_gap", 1e-6)
    arcs = {(a, b): model.addIntegral(lb=0, ub=1) for a in nodes[:-1] for b in nodes[1:] if a != b}
    visit = {b: model.addIntegral(lb=0, ub=1) for b in branches}
    order = {b: model.addVariable(lb=1, ub=max(n, 1)) for b in branches}
    carry = {pair: model.addIntegral(lb=0, ub=1) for pair in pairs}
    model.addConstr(sum(arcs[("<start>", b)] for b in nodes[1:]) == 1)
    model.addConstr(sum(arcs[(a, "<end>")] for a in nodes[:-1]) == 1)
    for b in branches:
        model.addConstr(sum(arcs[(a, b)] for a in nodes[:-1] if a != b) == visit[b])
        model.addConstr(sum(arcs[(b, c)] for c in nodes[1:] if c != b) == visit[b])
        model.addConstr(visit[b] <= sum(var for pair, var in carry.items() if b in pair))
    for a in branches:
        for b in branches:
            if a != b:
                model.addConstr(order[a] - order[b] + n * arcs[(a, b)] <= n - 1)
    for (source, target), var in carry.items():
        if source != base:
            model.addConstr(var <= visit[source])
        if target != base:
            model.addConstr(var <= visit[target])
        if source != base and target != base:
            model.addConstr(order[source] - order[target] + n * var <= n - 1)   # pickup first
    drive = [(arc, matrix.leg(location[a], location[b])) for (a, b), arc in arcs.items()]
    model.addConstr(sum(leg.minutes * arc for arc, leg in drive) + settings.stop_minutes * sum(visit.values()) <= settings.shift_hours * 60)
    per_km = settings.cost_per_km + TIE_BREAK_PER_KM
    model.maximize(sum(pair_value[pair] * var for pair, var in carry.items()) - per_km * sum(leg.km * arc for arc, leg in drive))
    model.run()
    if model.getModelStatus() not in (highspy.HighsModelStatus.kOptimal, highspy.HighsModelStatus.kTimeLimit) or not model.getInfo().primal_solution_status:
        return [base, base], set()
    successor = {a: b for (a, b), arc in arcs.items() if model.val(arc) > 0.5}
    tour, here = [base], "<start>"
    for _ in range(len(nodes)):
        here = successor.get(here, "<end>")
        tour.append(location[here])
        if here == "<end>":
            break
    return tour, {pair for pair, var in carry.items() if model.val(var) > 0.5}


def _sequence_value(sequence: list[str], base: str, pairs: list[tuple[str, str]], pair_value: dict[tuple[str, str], float],
                    matrix: DistanceMatrix, settings: AnalysisSettings) -> tuple[float, float, set[tuple[str, str]]]:
    """(net value, minutes, carried pairs) of visiting `sequence` between leaving and returning to the base.

    A pair rides when its sender comes before its receiver somewhere on the tour; the base is both
    the first and the last stop.
    """
    tour = [base, *sequence, base]
    first: dict[str, int] = {}
    last: dict[str, int] = {}
    for index, branch in enumerate(tour):
        first.setdefault(branch, index)
        last[branch] = index
    carried = {pair for pair in pairs if pair[0] in first and pair[1] in last and first[pair[0]] < last[pair[1]]}
    km, minutes = _tour_cost(tour, matrix, settings.stop_minutes)
    value = sum(pair_value[pair] for pair in carried) - (settings.cost_per_km + TIE_BREAK_PER_KM) * km
    return value, minutes, carried


def _local_search(base: str, pairs: list[tuple[str, str]], pair_value: dict[tuple[str, str], float], matrix: DistanceMatrix,
                  settings: AnalysisSettings, start: list[str], rounds: int) -> list[str]:
    """Iterated local search over the order of stops: add, remove, move, swap or replace a stop,
    keep the best tour within the shift, then shake it and search again. Fixed number of rounds
    and a fixed random seed, so the same data always gives the same tour."""
    import random

    limit = settings.shift_hours * 60 + 1e-6
    branches = sorted({b for pair in pairs for b in pair} - {base})

    def score(sequence: list[str]) -> float:
        value, minutes, _carried = _sequence_value(sequence, base, pairs, pair_value, matrix, settings)
        return value if minutes <= limit else float("-inf")

    def neighbours(sequence: list[str]):
        n = len(sequence)
        for i in range(n + 1):
            for branch in branches:
                if (i == 0 or sequence[i - 1] != branch) and (i == n or sequence[i] != branch):
                    yield sequence[:i] + [branch] + sequence[i:]
        for i in range(n):
            yield sequence[:i] + sequence[i + 1:]
            for branch in branches:
                if branch != sequence[i]:
                    yield sequence[:i] + [branch] + sequence[i + 1:]
            for j in range(n):
                if i != j:
                    moved = sequence[:i] + sequence[i + 1:]
                    yield moved[:j] + [sequence[i]] + moved[j:]
            for j in range(i + 1, n):
                swapped = list(sequence)
                swapped[i], swapped[j] = swapped[j], swapped[i]
                yield swapped

    def descend(sequence: list[str]) -> tuple[list[str], float]:
        current, current_score = sequence, score(sequence)
        while True:
            best, best_score = current, current_score
            for candidate in neighbours(current):
                candidate_score = score(candidate)
                if candidate_score > best_score + 1e-9:
                    best, best_score = candidate, candidate_score
            if best is current:
                return current, current_score
            current, current_score = best, best_score

    rng = random.Random(7)
    best, best_score = descend(start)
    current = best
    for _ in range(rounds):
        shaken = list(current)
        for _ in range(min(2, len(shaken))):
            shaken.pop(rng.randrange(len(shaken)))
        if branches:
            shaken.insert(rng.randrange(len(shaken) + 1), rng.choice(branches))
        candidate, candidate_score = descend(shaken)
        if candidate_score > best_score + 1e-9:
            best, best_score, current = candidate, candidate_score, candidate
        elif candidate_score > score(current) - 1e-9:
            current = candidate
        else:
            current = best
    return best


def solve(base: str, candidates: list[Candidate], matrix: DistanceMatrix, settings: AnalysisSettings) -> tuple[list[str], dict[tuple[str, str], tuple[int, int]], str]:
    """Choose what to carry and the order of stops. Sets candidate.carried.

    Returns the tour (base ... base; a branch may appear twice when A-to-B and B-to-A both ride),
    for each carried branch pair the positions of its pickup and delivery in the tour, and how it was
    solved: "exact" (proven best tour visiting each branch once, small chains) or "search".

    The vehicle has no capacity limit, so once it goes from A to B every A-to-B item rides along:
    each branch pair is decided as one, worth the sum of its items.
    """
    if not candidates:
        return [base, base], {}, "nothing"
    pair_value: dict[tuple[str, str], float] = {}
    for candidate in candidates:
        key = (candidate.transfer.from_branch, candidate.transfer.to_branch)
        pair_value[key] = pair_value.get(key, 0.0) + candidate.value
    pairs = sorted(pair_value)
    branch_count = len({b for pair in pairs for b in pair} - {base})
    method = "search"
    start: list[str] = []
    if branch_count <= EXACT_MAX_BRANCHES:
        tour, _chosen = _single_visit_plan(base, pairs, pair_value, matrix, settings, 30.0)
        start, method = tour[1:-1], "exact"
    # The search may also visit a branch twice; it starts from the exact tour when there is one.
    sequence = _local_search(base, pairs, pair_value, matrix, settings, start, SEARCH_ROUNDS if branch_count > EXACT_MAX_BRANCHES else 10)
    exact_score = _sequence_value(start, base, pairs, pair_value, matrix, settings)[0]
    found_score = _sequence_value(sequence, base, pairs, pair_value, matrix, settings)[0]
    if method == "exact" and found_score > exact_score + 1e-9:
        method = "exact+"   # a second visit to a branch beat the best single-visit tour
    elif method == "exact":
        sequence = start
    _value, _minutes, carried = _sequence_value(sequence, base, pairs, pair_value, matrix, settings)
    tour = [base, *sequence, base]
    positions = {}
    for pair in carried:
        pickup = tour.index(pair[0]) if pair[0] != base else 0
        delivery = len(tour) - 1 - tour[::-1].index(pair[1]) if pair[1] != base else len(tour) - 1
        positions[pair] = (pickup, delivery)
    for candidate in candidates:
        candidate.carried = (candidate.transfer.from_branch, candidate.transfer.to_branch) in positions
    return tour, positions, method


def carry_everything_tour(base: str, pairs: list[tuple[str, str]], matrix: DistanceMatrix) -> list[str]:
    """A tour carrying every transfer, for comparison. A branch may be visited twice (A to B and
    B to A on the same day), so pairs are added one by one at their cheapest place, pickup first."""
    tour = [base, base]
    for source, target in sorted(pairs, key=lambda p: -(matrix.leg(base, p[0]).km + matrix.leg(p[0], p[1]).km)):
        tour = _insert_pair(tour, source, target, matrix)[1]
    return tour


def _insert_pair(tour: list[str], source: str, target: str, matrix: DistanceMatrix) -> tuple[float, list[str]]:
    def insert(path: list[str], branch: str, after: int) -> tuple[float, list[str], int]:
        best = (float("inf"), path, after)
        for i in range(after + 1, len(path)):
            extra = matrix.leg(path[i - 1], branch).km + matrix.leg(branch, path[i]).km - matrix.leg(path[i - 1], path[i]).km
            if extra < best[0]:
                best = (extra, path[:i] + [branch] + path[i:], i)
        return best

    base = tour[0]
    path, extra = list(tour), 0.0
    if source == base:
        at = 0
    elif source in path[1:-1]:
        at = path.index(source, 1)
    else:
        added, path, at = insert(path, source, 0)
        extra += added
    if target != base and target not in path[at + 1:-1]:
        added, path, _ = insert(path, target, at)
        extra += added
    return extra, path


def cheapest_detour(tour: list[str], source: str, target: str, matrix: DistanceMatrix) -> float:
    """Extra km to add a pickup at source and then a delivery at target to the tour (base ... base)."""
    return round(_insert_pair(tour, source, target, matrix)[0], 1)


def plan_trip(transfers: list[Transfer], rows: list[dict[str, Any]], settings: AnalysisSettings, matrix: DistanceMatrix,
              base: str, stale: set[str]) -> TripPlan:
    candidates = transfer_values(transfers, rows, settings)
    located = set(matrix.branches)
    excluded: dict[str, str] = {}
    usable = []
    for candidate in candidates:
        for branch in (candidate.transfer.from_branch, candidate.transfer.to_branch):
            if branch not in located:
                excluded[branch] = "no_location"
            elif branch in stale:
                excluded[branch] = "stale"
        reasons = [excluded.get(b) for b in (candidate.transfer.from_branch, candidate.transfer.to_branch) if excluded.get(b)]
        if reasons:
            candidate.reason = reasons[0]
        else:
            usable.append(candidate)
    separate_pairs = {(c.transfer.from_branch, c.transfer.to_branch) for c in usable}
    if not base or base not in located:
        return TripPlan(base, [], 0, 0, 0, 0, candidates, excluded, None, None, 0, len(separate_pairs), "no_base")
    separate_km = sum(matrix.leg(base, a).km + matrix.leg(a, b).km + matrix.leg(b, base).km for a, b in separate_pairs)
    all_km = all_minutes = None
    if usable:
        # The shortest tour that carries every transfer, found by the same search with no time limit
        # and every pair made too valuable to drop, starting from a simple insertion tour.
        pairs = sorted(separate_pairs)
        forced = replace(settings, shift_hours=10_000.0, cost_per_km=1.0)
        start = carry_everything_tour(base, pairs, matrix)[1:-1]
        sequence = _local_search(base, pairs, {pair: 1e7 for pair in pairs}, matrix, forced, start, 10)
        everything = min(([base, *sequence, base], [base, *start, base]), key=lambda t: _tour_cost(t, matrix, settings.stop_minutes)[0])
        all_km, all_minutes = _tour_cost(everything, matrix, settings.stop_minutes)
    tour, positions, method = solve(base, usable, matrix, settings)
    km, minutes = _tour_cost(tour, matrix, settings.stop_minutes)
    stops, clock = [], 0.0
    for previous, branch in zip(tour, tour[1:-1]):
        clock += matrix.leg(previous, branch).minutes
        stops.append(Stop(branch, clock))
        clock += settings.stop_minutes if branch != base else 0
    load_at_base, unload_at_base = [], []
    for candidate in usable:
        if candidate.carried:
            pickup, delivery = positions[(candidate.transfer.from_branch, candidate.transfer.to_branch)]
            candidate.pickup_at, candidate.drop_at = pickup - 1, delivery - 1
            if pickup == 0:
                load_at_base.append(candidate)
            else:
                stops[pickup - 1].pickups.append(candidate)
            if delivery == len(tour) - 1:
                unload_at_base.append(candidate)
            else:
                stops[delivery - 1].drops.append(candidate)
        else:
            candidate.detour_km = cheapest_detour(tour, candidate.transfer.from_branch, candidate.transfer.to_branch, matrix)
            extra_cost = candidate.detour_km * settings.cost_per_km
            if not candidate.transfer.receiver_urgent and candidate.urgent_value < candidate.value * 0.5:
                candidate.reason = "wait"          # nothing runs out soon: it can ride on a later trip
            elif settings.cost_per_km > 0 and candidate.value < extra_cost:
                candidate.reason = "not_worth"
            else:
                candidate.reason = "no_time"
    value = sum(c.value for c in usable if c.carried)
    if usable and all(c.carried for c in usable) and all_km is not None and km < all_km:
        all_km, all_minutes = km, minutes
    return TripPlan(base, stops, round(km, 1), round(minutes), round(value, 2), round(km * settings.cost_per_km, 2), candidates,
                    excluded, round(all_km, 1) if all_km is not None else None, round(all_minutes) if all_minutes is not None else None,
                    round(separate_km, 1), len(separate_pairs), "solved" if any(c.carried for c in usable) else "nothing",
                    method=method, load_at_base=load_at_base, unload_at_base=unload_at_base)
