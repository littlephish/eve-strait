"""Jump range, fuel and fatigue/timer maths.

References: EVE University wiki "Jump drives" and CCP support article
"Jump Activation Cooldown and Jump Fatigue".
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..data.ships import Ship, Skills

# Caps (minutes)
FATIGUE_CAP_MIN = 5 * 60.0       # jump fatigue maxes at 5 hours
COOLDOWN_CAP_MIN = 30.0          # reactivation timer maxes at 30 minutes
GATE_TRAVEL_MIN = 1.0            # rough per-gate-jump travel time (align + jump)


def reactivation_floor(distance_ly: float) -> float:
    """Lowest possible reactivation timer for a jump of this length (minutes)."""
    return 1.0 + max(0.0, distance_ly)


def fatigue_floor_threshold(distance_ly: float) -> float:
    """If pre-jump fatigue is at/below this, the reactivation timer is at floor.

    reactivation = max(1+ly, fatigue/10); it stays at 1+ly while
    fatigue <= 10*(1+ly).
    """
    return 10.0 * (1.0 + max(0.0, distance_ly))


def max_range_ly(ship: Ship, skills: Skills) -> float:
    """Effective jump range. +20% per Jump Drive Calibration level."""
    return ship.base_range_ly * (1.0 + 0.20 * skills.jump_drive_calibration)


def fuel_per_ly(ship: Ship, skills: Skills) -> float:
    """Isotopes per light year after skill reductions."""
    factor = 1.0 - 0.10 * skills.jump_fuel_conservation
    if ship.is_freighter:
        factor *= 1.0 - 0.10 * skills.jump_freighters
    return ship.base_fuel_per_ly * factor


def fuel_for_jump(ship: Ship, skills: Skills, distance_ly: float) -> int:
    return math.ceil(fuel_per_ly(ship, skills) * distance_ly)


@dataclass
class JumpResult:
    distance_ly: float
    fuel: int
    cooldown_min: float          # reactivation timer you must wait after this jump
    fatigue_after_min: float     # jump fatigue immediately after this jump
    in_range: bool


def apply_jump(distance_ly: float, current_fatigue_min: float, skills: Skills) -> tuple[float, float]:
    """Return (reactivation_cooldown_min, new_fatigue_min) for one jump.

    - reactivation cooldown = max(1 + ly, pre-jump fatigue / 10), capped 30 min
    - new fatigue = max(10 * (1 + ly), pre-jump fatigue * (1 + ly)), capped 5 h
    - fatigue reduction (implants/boosters) reduces the accrued fatigue
    """
    ly = max(0.0, distance_ly)
    cooldown = max(1.0 + ly, current_fatigue_min / 10.0)
    cooldown = min(cooldown, COOLDOWN_CAP_MIN)

    reduction = 1.0 - skills.fatigue_reduction_pct / 100.0
    new_fatigue = max(10.0 * (1.0 + ly), current_fatigue_min * (1.0 + ly)) * reduction
    new_fatigue = min(new_fatigue, FATIGUE_CAP_MIN)
    return cooldown, new_fatigue


def evaluate_jump(
    ship: Ship,
    skills: Skills,
    distance_ly: float,
    current_fatigue_min: float,
) -> JumpResult:
    cooldown, new_fatigue = apply_jump(distance_ly, current_fatigue_min, skills)
    return JumpResult(
        distance_ly=distance_ly,
        fuel=fuel_for_jump(ship, skills, distance_ly),
        cooldown_min=cooldown,
        fatigue_after_min=new_fatigue,
        in_range=distance_ly <= max_range_ly(ship, skills) + 1e-9,
    )
