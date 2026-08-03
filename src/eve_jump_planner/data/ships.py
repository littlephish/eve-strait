"""Jump-capable ship data and pilot skills.

All numbers here are DATA, not hard-coded logic, so they are easy to correct
when CCP rebalances.  They are seeded from the EVE University wiki
(https://wiki.eveuniversity.org/Jump_drives).  ``base_range_ly`` is the
skill-0 range; effective range = base * (1 + 0.20 * JumpDriveCalibration).
``base_fuel_per_ly`` is isotopes/ly before skill reductions.

If a value looks off for the current patch, edit it here or override the
range/fuel directly in the UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Isotope type is only informative (which fuel to load); it does not change math.
ISOTOPE_BY_RACE = {
    "Amarr": "Helium Isotopes",
    "Caldari": "Nitrogen Isotopes",
    "Gallente": "Oxygen Isotopes",
    "Minmatar": "Hydrogen Isotopes",
    "ORE": "Oxygen Isotopes",
}


@dataclass(frozen=True)
class Ship:
    name: str
    hull_class: str
    race: str
    base_range_ly: float
    base_fuel_per_ly: float
    is_freighter: bool = False
    # Ships that can open a bridge/portal for others, and the bridge max range.
    bridge_range_ly: float | None = None

    @property
    def isotope(self) -> str:
        return ISOTOPE_BY_RACE.get(self.race, "Isotopes")


# NOTE: base_fuel_per_ly for non-freighter capitals is approximate; the four
# jump freighters use their per-hull consumption values.  Everything is editable.
SHIPS: list[Ship] = [
    # --- Jump Freighters ---------------------------------------------------
    Ship("Ark",    "Jump Freighter", "Amarr",    5.0, 8800.0, is_freighter=True),
    Ship("Rhea",   "Jump Freighter", "Caldari",  5.0, 10000.0, is_freighter=True),
    Ship("Anshar", "Jump Freighter", "Gallente", 5.0, 9400.0, is_freighter=True),
    Ship("Nomad",  "Jump Freighter", "Minmatar", 5.0, 8200.0, is_freighter=True),
    # --- Rorqual -----------------------------------------------------------
    Ship("Rorqual", "Capital Industrial", "ORE", 5.0, 10000.0, is_freighter=True),
    # --- Carriers ----------------------------------------------------------
    Ship("Archon",     "Carrier", "Amarr",    3.0, 1000.0),
    Ship("Chimera",    "Carrier", "Caldari",  3.0, 1000.0),
    Ship("Thanatos",   "Carrier", "Gallente", 3.0, 1000.0),
    Ship("Nidhoggur",  "Carrier", "Minmatar", 3.0, 1000.0),
    # --- Force Auxiliary ---------------------------------------------------
    Ship("Apostle", "Force Auxiliary", "Amarr",    3.0, 1000.0),
    Ship("Minokawa","Force Auxiliary", "Caldari",  3.0, 1000.0),
    Ship("Ninazu",  "Force Auxiliary", "Gallente", 3.0, 1000.0),
    Ship("Lif",     "Force Auxiliary", "Minmatar", 3.0, 1000.0),
    # --- Dreadnoughts ------------------------------------------------------
    Ship("Revelation", "Dreadnought", "Amarr",    3.5, 1000.0),
    Ship("Phoenix",    "Dreadnought", "Caldari",  3.5, 1000.0),
    Ship("Moros",      "Dreadnought", "Gallente", 3.5, 1000.0),
    Ship("Naglfar",    "Dreadnought", "Minmatar", 3.5, 1000.0),
    # --- Supercarriers -----------------------------------------------------
    Ship("Aeon",   "Supercarrier", "Amarr",    3.0, 1000.0),
    Ship("Wyvern", "Supercarrier", "Caldari",  3.0, 1000.0),
    Ship("Nyx",    "Supercarrier", "Gallente", 3.0, 1000.0),
    Ship("Hel",    "Supercarrier", "Minmatar", 3.0, 1000.0),
    # --- Titans (also bridge, 6 ly) ----------------------------------------
    Ship("Avatar",     "Titan", "Amarr",    3.0, 1000.0, bridge_range_ly=6.0),
    Ship("Leviathan",  "Titan", "Caldari",  3.0, 1000.0, bridge_range_ly=6.0),
    Ship("Erebus",     "Titan", "Gallente", 3.0, 1000.0, bridge_range_ly=6.0),
    Ship("Ragnarok",   "Titan", "Minmatar", 3.0, 1000.0, bridge_range_ly=6.0),
    # --- Black Ops (covert bridge, 8 ly) -----------------------------------
    Ship("Redeemer", "Black Ops", "Amarr",    4.0, 500.0, bridge_range_ly=8.0),
    Ship("Widow",    "Black Ops", "Caldari",  4.0, 500.0, bridge_range_ly=8.0),
    Ship("Sin",      "Black Ops", "Gallente", 4.0, 500.0, bridge_range_ly=8.0),
    Ship("Panther",  "Black Ops", "Minmatar", 4.0, 500.0, bridge_range_ly=8.0),
]

SHIPS_BY_NAME = {s.name: s for s in SHIPS}


@dataclass
class Skills:
    """Pilot skills / implants affecting jump drives."""

    jump_drive_calibration: int = 4      # +20% range per level
    jump_drive_operation: int = 5        # -5% capacitor need per level (info only)
    jump_fuel_conservation: int = 4      # -10% fuel per level
    jump_freighters: int = 4             # -10% fuel per level (freighters only)
    fatigue_reduction_pct: float = 0.0   # implants/boosters, 0-100

    def clamp(self) -> "Skills":
        self.jump_drive_calibration = max(0, min(5, self.jump_drive_calibration))
        self.jump_drive_operation = max(0, min(5, self.jump_drive_operation))
        self.jump_fuel_conservation = max(0, min(5, self.jump_fuel_conservation))
        self.jump_freighters = max(0, min(5, self.jump_freighters))
        self.fatigue_reduction_pct = max(0.0, min(100.0, self.fatigue_reduction_pct))
        return self
