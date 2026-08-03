"""Route validation, timeline simulation and jump pathfinding."""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from ..data import docking
from ..data.ships import Ship, Skills
from ..data.universe import System, Universe
from . import mechanics


@dataclass
class Leg:
    src: System
    dst: System
    mode: str                    # "jump" or "gate"
    distance_ly: float
    fuel: int
    in_range: bool
    cooldown_min: float          # reactivation timer produced by this jump
    fatigue_after_min: float
    wait_before_min: float       # time waited at src before this leg
    t_depart_min: float          # minutes from start when this leg fires


@dataclass
class RoutePlan:
    legs: list[Leg] = field(default_factory=list)
    total_fuel: int = 0
    total_time_min: float = 0.0
    peak_fatigue_min: float = 0.0
    peak_reactivation_min: float = 0.0
    all_in_range: bool = True

    @property
    def jumps(self) -> int:
        return sum(1 for leg in self.legs if leg.mode == "jump")

    @property
    def gates(self) -> int:
        return sum(1 for leg in self.legs if leg.mode == "gate")


def simulate(
    ship: Ship,
    skills: Skills,
    waypoints: list[System],
    modes: list[str] | None = None,
    strategy: str = "min_time",
    start_fatigue_min: float = 0.0,
) -> RoutePlan:
    """Simulate travelling an ordered list of systems (jumps and/or gates).

    ``strategy``:
      * ``"min_time"``          — wait only the jump reactivation timer.
      * ``"min_reactivation"``  — before each jump also wait for fatigue to
        decay to the point where that jump's reactivation timer is at its
        floor (1 + ly). Longer total time, but each blue timer stays minimal.

    Fatigue and the reactivation (blue) timer both decay 1:1 with real time.
    Gate hops don't use fuel, add no fatigue, and aren't blocked by the blue
    timer, but time still passes (letting timers decay).
    """
    n = len(waypoints) - 1
    if modes is None:
        modes = ["jump"] * max(0, n)
    plan = RoutePlan()
    fatigue = start_fatigue_min
    cooldown_remaining = 0.0
    clock = 0.0

    for i, (src, dst) in enumerate(zip(waypoints, waypoints[1:])):
        mode = modes[i] if i < len(modes) else "jump"
        dist = Universe.distance_ly(src, dst)

        if mode == "gate":
            wait = 0.0
            travel = mechanics.GATE_TRAVEL_MIN
            clock += wait
            fatigue = max(0.0, fatigue - travel)
            cooldown_remaining = max(0.0, cooldown_remaining - travel)
            plan.legs.append(Leg(src, dst, "gate", dist, 0, True, 0.0,
                                 fatigue, wait, clock))
            clock += travel
            continue

        # jump: wait out the blue timer, plus (optionally) let fatigue decay so
        # this jump's reactivation timer is at floor.
        wait = cooldown_remaining
        if strategy == "min_reactivation":
            threshold = mechanics.fatigue_floor_threshold(dist)
            wait = max(wait, fatigue - threshold)
        wait = max(0.0, wait)
        clock += wait
        fatigue = max(0.0, fatigue - wait)
        cooldown_remaining = max(0.0, cooldown_remaining - wait)

        res = mechanics.evaluate_jump(ship, skills, dist, fatigue)
        plan.legs.append(Leg(src, dst, "jump", dist, res.fuel, res.in_range,
                             res.cooldown_min, res.fatigue_after_min, wait, clock))
        plan.total_fuel += res.fuel
        plan.peak_fatigue_min = max(plan.peak_fatigue_min, res.fatigue_after_min)
        plan.peak_reactivation_min = max(plan.peak_reactivation_min, res.cooldown_min)
        if not res.in_range:
            plan.all_in_range = False
        fatigue = res.fatigue_after_min
        cooldown_remaining = res.cooldown_min

    plan.total_time_min = clock
    return plan


def find_path(
    universe: Universe,
    ship: Ship,
    skills: Skills,
    origin: System,
    destination: System,
    minimize: str = "jumps",
    can_land=None,
    max_expansions: int = 60_000,
) -> list[System] | None:
    """Shortest jump path from origin to destination.

    ``minimize`` = "jumps" (fewest hops) or "fuel" (least total isotopes).
    Only systems with security < 0.5 may be used as intermediate/target
    landings (the origin may be anywhere).  ``can_land`` is an optional
    predicate ``System -> bool`` (e.g. "has a dock my hull can use"); the
    origin and destination are always allowed.
    """
    rng = mechanics.max_range_ly(ship, skills)
    if rng <= 0:
        return None

    systems = list(universe.systems.values())

    def landable(s: System) -> bool:
        if s.id == destination.id or can_land is None:
            return True
        return can_land(s)

    def neighbors(node: System):
        for s, dist in universe.within_range(node, rng, jumpable_only=True):
            if s.id == destination.id or (s.jumpable and landable(s)):
                yield s, dist

    def cost(dist: float) -> float:
        return 1.0 if minimize == "jumps" else mechanics.fuel_for_jump(ship, skills, dist)

    best: dict[int, float] = {origin.id: 0.0}
    prev: dict[int, int] = {}
    pq: list[tuple[float, int]] = [(0.0, origin.id)]
    id_map = {s.id: s for s in systems}
    id_map[origin.id] = origin
    id_map[destination.id] = destination
    expansions = 0

    while pq:
        c, nid = heapq.heappop(pq)
        if nid == destination.id:
            # reconstruct
            path = [id_map[nid]]
            while nid in prev:
                nid = prev[nid]
                path.append(id_map[nid])
            path.reverse()
            return path
        if c > best.get(nid, float("inf")):
            continue
        expansions += 1
        if expansions > max_expansions:
            return None
        node = id_map[nid]
        for s, dist in neighbors(node):
            nc = c + cost(dist)
            if nc < best.get(s.id, float("inf")):
                best[s.id] = nc
                prev[s.id] = nid
                id_map[s.id] = s
                heapq.heappush(pq, (nc, s.id))
    return None


# Weights for the combined jump+gate search. A jump is scarce (fuel + fatigue),
# so it is worth avoiding many gate hops; gates are cheap but slow.
_JUMP_W = 100.0
_GATE_W = 1.0


def plan_multimodal(
    universe: Universe,
    ship: Ship,
    skills: Skills,
    origin: System,
    destination: System,
    minimize: str = "jumps",
    can_land=None,
    max_expansions: int = 200_000,
) -> tuple[list[System], list[str]] | None:
    """Best origin->destination path mixing capital jumps and stargate hops.

    Enforces EVE rules:
      * a jump can only *originate from* and *land in* security < 0.5;
      * capitals/supers cannot use high-sec gates at all (only jump freighters
        and other subcap hulls may gate through high-sec).

    Returns (systems, modes) where modes[i] is how leg i (systems[i]->[i+1])
    is travelled ("jump" or "gate"), or None if unreachable.
    """
    rng = mechanics.max_range_ly(ship, skills)

    def landable(s: System) -> bool:
        return s.id == destination.id or can_land is None or can_land(s)

    def jump_fuel_w(dist: float) -> float:
        # tiny fuel tiebreak so equal-jump routes prefer cheaper fuel
        extra = mechanics.fuel_for_jump(ship, skills, dist) / 1e6 if minimize == "fuel" else 0.0
        return _JUMP_W + extra

    best: dict[int, float] = {origin.id: 0.0}
    prev: dict[int, tuple[int, str]] = {}
    pq: list[tuple[float, int]] = [(0.0, origin.id)]
    expansions = 0

    while pq:
        c, nid = heapq.heappop(pq)
        if nid == destination.id:
            path_ids = [nid]
            modes: list[str] = []
            while nid in prev:
                nid, mode = prev[nid]
                path_ids.append(nid)
                modes.append(mode)
            path_ids.reverse()
            modes.reverse()
            return [universe.systems[i] for i in path_ids], modes
        if c > best.get(nid, float("inf")):
            continue
        expansions += 1
        if expansions > max_expansions:
            return None
        node = universe.systems[nid]

        # Gate edges (respect high-sec restriction for capitals).
        for gid in universe.gates.get(nid, ()):
            g = universe.systems.get(gid)
            if g is None or not docking.gate_allowed(ship, g.security):
                continue
            nc = c + _GATE_W
            if nc < best.get(gid, float("inf")):
                best[gid] = nc
                prev[gid] = (nid, "gate")
                heapq.heappush(pq, (nc, gid))

        # Jump edges (only from low/null, only into low/null).
        if rng > 0 and node.security < 0.5:
            for s, dist in universe.within_range(node, rng, jumpable_only=True):
                if not (s.id == destination.id or landable(s)):
                    continue
                nc = c + jump_fuel_w(dist)
                if nc < best.get(s.id, float("inf")):
                    best[s.id] = nc
                    prev[s.id] = (nid, "jump")
                    heapq.heappush(pq, (nc, s.id))
    return None
