"""Ansiblex capacitor economics, from Cradle of War (2026-09-22).

An Ansiblex no longer burns liquid ozone or charges a toll. It holds a
capacitor -- 1250 TJ, recharging non-linearly at roughly 200 TJ/hour, about
6.25 hours from empty -- and every ship through it drains an amount set by the
ship's class and by how far the *destination* sits from the owning alliance's
capital system. At zero the gate stops working until it recovers.

Two deliberate omissions.

**Live capacitor state is not modelled.** A gate's charge is shared, depleting
and exposed nowhere in ESI. A simulated figure would be wrong the moment
anybody else jumps, so this module answers "what does this activation cost" and
never "will it work".

**Cost is reported, not routed on.** Zone 1's multiplier is zero, so folding it
into the router's edge cost would make in-zone hops free and let the router
chain them endlessly. A hop stays a hop; this number is metadata.

Cost is also directional: the zone comes from the destination, so the same gate
is expensive outbound from the capital and cheap coming home.
"""
from __future__ import annotations

# Upper bound in light years for zones 1-4; anything beyond is zone 5.
ZONE_BOUNDS = (5.0, 10.0, 15.0, 20.0)

# Multiplier applied to the hull's base cost, indexed by zone. Zone 1 is free,
# which is what lets an alliance move freely inside its own core.
ZONE_MULTIPLIER = {1: 0.0, 2: 2.0, 3: 6.0, 4: 9.0, 5: 15.0}

# Base TJ per activation, by this app's hull_class. Only hulls the app models
# appear here. "Subcapital" is a single generic stand-in covering classes whose
# real costs range from 1.00 (freighter, hauler) to 19.00 (Marauder), so it is
# deliberately absent rather than answered wrongly.
#
# Hulls barred from the network entirely are also absent -- there is no cost
# for a jump that cannot happen.
BASE_TJ = {
    "Jump Freighter": 1.0,
    "Black Ops": 18.0,
    "Capital Industrial": 19.0,       # Rorqual
}

# CCP's table adjusts the Rorqual away from its own multiplier in the two
# middle zones: 57.00 rather than 19x2=38, and 104.50 rather than 19x6=114.
# Zones 4 and 5 follow the multiplier normally. Overrides are
# (hull_class, zone) -> absolute TJ.
COST_OVERRIDES = {
    ("Capital Industrial", 2): 57.0,
    ("Capital Industrial", 3): 104.5,
}


def zone_for(distance_ly: float) -> int:
    """Which capacitor zone a destination that far from the capital sits in."""
    for i, bound in enumerate(ZONE_BOUNDS, start=1):
        if distance_ly <= bound:
            return i
    return 5


def capacitor_cost(hull_class: str, distance_ly: float) -> float | None:
    """TJ drained by one activation, or None where it cannot be known.

    None means unknowable rather than free: a hull class this app does not
    model per-class, or one barred from Ansiblex entirely. Callers must show
    it as unknown, never as zero -- zone 1 really does cost nothing, and
    conflating the two would print "free" for a Titan.
    """
    base = BASE_TJ.get(hull_class)
    if base is None:
        return None
    zone = zone_for(distance_ly)
    override = COST_OVERRIDES.get((hull_class, zone))
    if override is not None:
        return override
    return base * ZONE_MULTIPLIER[zone]
