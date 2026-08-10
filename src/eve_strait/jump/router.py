"""Route validation, timeline simulation and jump pathfinding."""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from ..data import docking
from ..data.ships import Ship, Skills
from ..data.universe import System, Universe
from ..esi import evescout
from . import mechanics


@dataclass
class Leg:
    src: System
    dst: System
    mode: str                    # "jump", "gate", "bridge" or "hole"
    distance_ly: float
    fuel: int
    in_range: bool
    cooldown_min: float          # reactivation timer produced by this jump
    fatigue_after_min: float
    wait_before_min: float       # time waited at src before this leg
    t_depart_min: float          # minutes from start when this leg fires
    reason: str = ""             # why an invalid jump can't happen


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

    @property
    def bridges(self) -> int:
        return sum(1 for leg in self.legs if leg.mode == "bridge")

    @property
    def holes(self) -> int:
        return sum(1 for leg in self.legs if leg.mode == "hole")


def simulate(
    ship: Ship,
    skills: Skills,
    waypoints: list[System],
    modes: list[str] | None = None,
    strategy: str = "min_time",
    start_fatigue_min: float = 0.0,
    universe: Universe | None = None,
) -> RoutePlan:
    """Simulate travelling an ordered list of systems (jumps and/or gates).

    ``strategy``:
      * ``"min_time"``          - wait only the jump reactivation timer.
      * ``"min_reactivation"``  - before each jump also wait for fatigue to
        decay to the point where that jump's reactivation timer is at its
        floor (1 + ly). Longer total time, but each blue timer stays minimal.

    Fatigue and the reactivation (blue) timer both decay 1:1 with real time.
    Gate hops don't use fuel, add no fatigue, and aren't blocked by the blue
    timer, but time still passes (letting timers decay).

    ``universe`` is only needed for wormhole legs, to tell a Turnur hole (one
    transit) from a Thera crossing (two). Without it a hole is timed as one,
    which under-states a Thera leg rather than inventing a number.
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

        if mode == "hole":
            # A wormhole is flown through, not jumped. No fuel, no fatigue, no
            # blue timer -- that is the whole reason it is worth routing over.
            # Only time passes, and timers decay while it does.
            info = universe.hole_between(src.id, dst.id) if universe else None
            travel = mechanics.GATE_TRAVEL_MIN * (info or {}).get("hops", 1)
            fatigue = max(0.0, fatigue - travel)
            cooldown_remaining = max(0.0, cooldown_remaining - travel)
            plan.legs.append(Leg(src, dst, "hole", dist, 0, True, 0.0,
                                 fatigue, 0.0, clock))
            clock += travel
            continue

        if mode == "bridge":
            # Ansiblex: the structure burns the fuel, not your ship, but the
            # pilot still takes jump fatigue and a reactivation timer.
            wait = cooldown_remaining
            if strategy == "min_reactivation":
                wait = max(wait, fatigue - mechanics.fatigue_floor_threshold(dist))
            wait = max(0.0, wait)
            clock += wait
            fatigue = max(0.0, fatigue - wait)
            cd, new_fatigue = mechanics.apply_jump(dist, fatigue, skills)
            plan.legs.append(Leg(src, dst, "bridge", dist, 0, True, cd,
                                 new_fatigue, wait, clock))
            plan.peak_fatigue_min = max(plan.peak_fatigue_min, new_fatigue)
            plan.peak_reactivation_min = max(plan.peak_reactivation_min, cd)
            fatigue, cooldown_remaining = new_fatigue, cd
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
        # You CAN jump out of high-sec; you cannot jump INTO high-sec (no cyno
        # can be lit there). So only the destination's security matters.
        if not dst.jumpable:
            reason = "can't jump into hi-sec"
        elif not res.in_range:
            reason = "out of range"
        else:
            reason = ""
        valid = reason == ""
        plan.legs.append(Leg(src, dst, "jump", dist, res.fuel, valid,
                             res.cooldown_min, res.fatigue_after_min, wait, clock,
                             reason))
        plan.total_fuel += res.fuel
        plan.peak_fatigue_min = max(plan.peak_fatigue_min, res.fatigue_after_min)
        plan.peak_reactivation_min = max(plan.peak_reactivation_min, res.cooldown_min)
        if not valid:
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
    landings (jumps may depart high-sec, but never land there). ``can_land`` is an optional
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


# Weight presets. A single jump covers many gate hops, so to make a jump
# freighter actually *jump* (rather than gate the whole way) a jump must not
# cost more than the gates it replaces.
#   "prefer_jump"  -> jump low, gate slightly higher: jump across low/null,
#                     gate only the forced high-sec tail. (default)
#   "prefer_gate"  -> jump expensive: gate wherever possible to save fuel and
#                     fatigue, jump only to cross gaps that gates can't.
_WEIGHTS = {
    "only_jumps": (1.0, 0.0),  # jumps only; gates disabled
    "jumps": (1.0, 1.3),       # prefer jumping
    "fuel": (20.0, 1.0),       # prefer gating (save fuel/fatigue)
}
# Extra gate cost by security preference (added per gate hop into that system).
_GATE_SEC_PENALTY = {"fast": 0.0, "safe": 6.0, "insecure": 6.0}


def plan_multimodal(
    universe: Universe,
    ship: Ship,
    skills: Skills,
    origin: System,
    destination: System,
    minimize: str = "jumps",
    gate_pref: str = "fast",
    jump_cost: float | None = None,
    use_ansiblex: bool = True,
    use_wormholes: bool = False,
    haven=None,
    haven_penalty: float = 0.35,
    jammed=None,
    danger=None,
    danger_penalty: float = 1.5,
    can_land=None,
    avoid: set | None = None,
    avoid_edges: set | None = None,
    max_expansions: int = 200_000,
) -> tuple[list[System], list[str]] | None:
    """Best origin->destination path mixing capital jumps and stargate hops.

    Enforces EVE rules:
      * a jump may originate anywhere (including high-sec) but can only
        *land in* security < 0.5 (no cyno can be lit in high-sec);
      * capitals/supers cannot use high-sec gates at all (only jump freighters
        and other subcap hulls may gate through high-sec).

    Returns (systems, modes) where modes[i] is how leg i (systems[i]->[i+1])
    is travelled ("jump" or "gate"), or None if unreachable.
    """
    rng = mechanics.max_range_ly(ship, skills)
    if jump_cost is not None:
        # "A jump is worth this many gate hops." Higher = take more gates to
        # avoid a jump (saving fuel and fatigue); 1 = gates and jumps equal.
        w_jump, w_gate = max(0.0, jump_cost), 1.0
    else:
        w_jump, w_gate = _WEIGHTS.get(minimize, _WEIGHTS["jumps"])
    allow_gates = minimize != "only_jumps"
    sec_penalty = _GATE_SEC_PENALTY.get(gate_pref, 0.0)
    avoid = avoid or set()

    banned_edges = avoid_edges or set()

    def blocked(sid: int) -> bool:
        return sid in avoid and sid != destination.id

    def edge_banned(a_id: int, b_id: int) -> bool:
        return (a_id, b_id) in banned_edges or (b_id, a_id) in banned_edges

    def gate_cost(sec: float) -> float:
        c = w_gate
        if gate_pref == "safe" and sec < 0.5:
            c += sec_penalty        # avoid low/null
        elif gate_pref == "insecure" and sec >= 0.5:
            c += sec_penalty        # avoid high-sec
        return c

    def landable(s: System) -> bool:
        return s.id == destination.id or can_land is None or can_land(s)

    def jump_fuel_w(dist: float) -> float:
        # tiny fuel tiebreak so equal-cost routes prefer shorter jumps
        return w_jump + mechanics.fuel_for_jump(ship, skills, dist) / 1e7

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
        if allow_gates:
            for gid in universe.gates.get(nid, ()):
                g = universe.systems.get(gid)
                if (g is None or blocked(gid) or edge_banned(nid, gid)
                        or not docking.gate_allowed(ship, g.security)):
                    continue
                nc = c + gate_cost(g.security)
                # Gate camps are the risk on gates, so the same bias applies.
                if danger is not None and gid != destination.id and danger(gid):
                    nc += danger_penalty
                if nc < best.get(gid, float("inf")):
                    best[gid] = nc
                    prev[gid] = (nid, "gate")
                    heapq.heappush(pq, (nc, gid))

        # Ansiblex jump-gate edges: one activation covers any distance, and
        # capitals can use them. Not blocked by "only jumps" -- an Ansiblex is
        # a jump, not a stargate.
        for bid in (universe.bridges.get(nid, ()) if use_ansiblex else ()):
            b = universe.systems.get(bid)
            if (b is None or blocked(bid) or edge_banned(nid, bid)
                    or not docking.gate_allowed(ship, b.security)):
                continue
            nc = c + w_gate
            if nc < best.get(bid, float("inf")):
                best[bid] = nc
                prev[bid] = (nid, "bridge")
                heapq.heappush(pq, (nc, bid))

        # Scouted Thera/Turnur wormholes. Taking one is like taking a gate: you
        # warp to it and jump through, burning no fuel, taking no fatigue and
        # lighting no cyno. So it is costed as a gate hop and answers to the
        # same controls -- "only jumps" excludes it, and the gate security
        # preference applies to where it puts you down. Not an Ansiblex, which
        # is a jump and carries a jump's fatigue.
        #
        # The one thing a gate does not have is a size limit; a hull too heavy
        # for the hole simply has no edge here.
        if use_wormholes and allow_gates:
            for hid in universe.holes.get(nid, ()):
                h = universe.systems.get(hid)
                info = universe.hole_between(nid, hid)
                if (h is None or info is None or blocked(hid)
                        or edge_banned(nid, hid)
                        or not docking.gate_allowed(ship, h.security)
                        or not evescout.fits(ship.hull_class, info)):
                    continue
                # A Thera crossing is two holes, so it costs two hops. Charging
                # one would make it look like a shortcut it is not.
                nc = c + gate_cost(h.security) * info.get("hops", 1)
                if danger is not None and hid != destination.id and danger(hid):
                    nc += danger_penalty
                if nc < best.get(hid, float("inf")):
                    best[hid] = nc
                    prev[hid] = (nid, "hole")
                    heapq.heappush(pq, (nc, hid))

        # Jump edges. You may jump OUT of high-sec, but never INTO high-sec
        # (a cyno can't be lit there) -- hence jumpable_only on the target.
        if rng > 0:
            for s, dist in universe.within_range(node, rng, jumpable_only=True):
                if blocked(s.id) or not (s.id == destination.id or landable(s)):
                    continue
                # A cyno jammer stops a cyno being lit, so you cannot jump into
                # that system at all. Hard constraint, not a preference; gates
                # into it are still fine.
                if jammed is not None and s.id in jammed:
                    continue
                # After a jump you are pinned by the reactivation timer, so
                # nudge landings toward systems with a tether or POS shield.
                exposed = (haven is not None and s.id != destination.id
                           and not haven(s.id))
                cost = jump_fuel_w(dist) + (haven_penalty if exposed else 0.0)
                # Recent player kills: steer around, but never make a route
                # impossible over it.
                if danger is not None and s.id != destination.id and danger(s.id):
                    cost += danger_penalty
                nc = c + cost
                if nc < best.get(s.id, float("inf")):
                    best[s.id] = nc
                    prev[s.id] = (nid, "jump")
                    heapq.heappush(pq, (nc, s.id))
    return None


def _plan_stats(ship, skills, systems, modes, strategy="min_time"):
    plan = simulate(ship, skills, systems, modes, strategy=strategy)
    return {
        "jumps": plan.jumps,
        "gates": plan.gates,
        "fuel": plan.total_fuel,
        "time_min": plan.total_time_min,
        "peak_fatigue": plan.peak_fatigue_min,
        "peak_reactivation": plan.peak_reactivation_min,
        "ok": plan.all_in_range,
        "systems": systems,
        "modes": modes,
    }


def gate_runs(systems, modes):
    """Contiguous stretches of gate travel.

    Returns (from, to, hops, edges) where ``edges`` are the (id, id) pairs the
    run actually traverses -- needed to ask "could I avoid this gate?".
    """
    runs, start, count = [], None, 0

    def close(start, count):
        edges = [(systems[i].id, systems[i + 1].id)
                 for i in range(start, start + count)]
        return (systems[start], systems[start + count], count, edges)

    for i, mode in enumerate(modes):
        if mode == "gate":
            if start is None:
                start = i
            count += 1
        elif start is not None:
            runs.append(close(start, count))
            start, count = None, 0
    if start is not None:
        runs.append(close(start, count))
    return runs


def analyze_gate_assist(universe, ship, skills, origin, destination,
                        gate_pref="fast", jump_cost=None, use_ansiblex=True,
                        use_wormholes=False,
                        haven=None, jammed=None, danger=None,
                        can_land=None, avoid=None, strategy="min_time"):
    """Quantify what stargates buy you on this route.

    Compares a pure jump route against the best jump+gate route, so you can
    see when a short gate run replaces several jumps (or makes an otherwise
    impossible high-sec origin/destination reachable at all).
    """
    def build(minimize, cost=None, holes=False):
        res = plan_multimodal(universe, ship, skills, origin, destination,
                              minimize=minimize, gate_pref=gate_pref,
                              jump_cost=cost, use_ansiblex=use_ansiblex,
                              use_wormholes=holes,
                              haven=haven, jammed=jammed, danger=danger,
                              can_land=can_land, avoid=avoid)
        if res is None:
            return None
        return _plan_stats(ship, skills, res[0], res[1], strategy)

    # The three baselines never use wormholes, so the comparison below is
    # honestly "what do the holes add" rather than a mix of the two.
    jump_only = build("only_jumps")
    mixed = build("jumps", jump_cost)
    gating = build("fuel")

    # What the scouted holes are worth, and which ones the route would use.
    # Only computed when asked: it is a second full search.
    holed = build("jumps", jump_cost, holes=True) if use_wormholes else None
    hole_legs = []
    if holed:
        for a, b, mode in zip(holed["systems"], holed["systems"][1:],
                              holed["modes"]):
            if mode != "hole":
                continue
            info = universe.hole_between(a.id, b.id) or {}
            hole_legs.append({
                "from": a, "to": b,
                "via": info.get("via", "?"),
                "sig_from": info.get("sigs", {}).get(a.id),
                "sig_to": info.get("sigs", {}).get(b.id),
                "max_t": info.get("max_t"),
                "hours": info.get("hours"),
            })

    runs = gate_runs(mixed["systems"], mixed["modes"]) if mixed else []

    # Which gate runs are unavoidable? Re-plan with the run's exit system
    # banned: if nothing gets through, that system is a hard chokepoint you
    # must gate through (the classic "I can jump most of the way, but this
    # one gate is the only link" case).
    annotated = []
    for a, b, hops, edges in runs:
        # Probed without wormholes on purpose: "mandatory" should mean the
        # gates are unavoidable in their own right, not that they became
        # avoidable thanks to a connection that expires this evening.
        probe = plan_multimodal(universe, ship, skills, origin, destination,
                                minimize="jumps", gate_pref=gate_pref,
                                jump_cost=jump_cost, use_ansiblex=use_ansiblex,
                                can_land=can_land, avoid=avoid,
                                avoid_edges=set(edges))
        annotated.append({
            "from": a, "to": b, "hops": hops,
            "span_ly": Universe.distance_ly(a, b),
            "mandatory": probe is None,
        })

    result = {"jump_only": jump_only, "mixed": mixed, "gating": gating,
              "runs": annotated, "holed": holed, "hole_legs": hole_legs}

    if holed and mixed:
        result["hole_saved"] = {
            "jumps": mixed["jumps"] - holed["jumps"],
            "gates": mixed["gates"] - holed["gates"],
            "fuel": mixed["fuel"] - holed["fuel"],
            "time_min": mixed["time_min"] - holed["time_min"],
            "fatigue": mixed["peak_fatigue"] - holed["peak_fatigue"],
        }
    elif holed and not mixed:
        result["hole_saved"] = None     # a hole is the only way through

    if mixed and jump_only:
        result["saved"] = {
            "jumps": jump_only["jumps"] - mixed["jumps"],
            "fuel": jump_only["fuel"] - mixed["fuel"],
            "time_min": jump_only["time_min"] - mixed["time_min"],
            "fatigue": jump_only["peak_fatigue"] - mixed["peak_fatigue"],
        }
    elif mixed and not jump_only:
        result["saved"] = None      # gates are the only way through
    return result


def route_through(universe, ship, skills, systems, minimize="jumps",
                  gate_pref="fast", jump_cost=None, use_ansiblex=True,
                  use_wormholes=False,
                  haven=None, jammed=None, danger=None,
                  can_land=None, avoid=None):
    """Route through an ordered list of REQUIRED waypoints, bridging each
    consecutive pair with jumps/gates. Every input waypoint is preserved as an
    anchor. Returns (systems, modes) or None if any leg is unreachable."""
    if len(systems) < 2:
        return list(systems), []
    full = [systems[0]]
    modes: list[str] = []
    for a, b in zip(systems, systems[1:]):
        res = plan_multimodal(universe, ship, skills, a, b, minimize=minimize,
                              gate_pref=gate_pref, jump_cost=jump_cost,
                              use_ansiblex=use_ansiblex,
                              use_wormholes=use_wormholes, haven=haven,
                              jammed=jammed, danger=danger,
                              can_land=can_land, avoid=avoid)
        if res is None:
            return None
        segs, segmodes = res           # segs[0] == a, segs[-1] == b
        full.extend(segs[1:])
        modes.extend(segmodes)
    return full, modes
