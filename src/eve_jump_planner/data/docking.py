"""Where a hull can dock, and whether that dock is *safe*.

Docking capability in EVE is by ship *category*, not size class alone:

    * Jump freighters / freighters / Orca / Bowhead dock as **subcaps** -> anywhere.
    * Capitals for docking = dreadnought, carrier, force auxiliary, capital
      industrial (Rorqual).
    * Supercapitals = supercarrier, titan -> **Keepstar only**.

Structure docking (Upwell):

    Astrahus (M) / Raitaru (M) / Athanor (M) / Azbel (L)  -> subcap docking only
    Fortizar (L) / Sotiyo (XL) / Tatara (XL)              -> capital docking
    Keepstar (XL)                                         -> supercapital docking

NPC stations: capitals may dock, supercaps may not.  Some NPC station *models*
are "kickout" stations (long undock runway, no docking ring) -> flagged unsafe.

A publicly-dockable player structure ("freeport", e.g. *Turnur - Summit's
Beacon*) is dockable but a **risk** (access can be revoked / hostile owner).

All tables below are editable data.
"""
from __future__ import annotations

from dataclasses import dataclass

from .ships import Ship

# Ship docking categories
SUBCAP = 0
CAPITAL = 1
SUPERCAP = 2
_CAT_NAME = {SUBCAP: "subcap", CAPITAL: "capital", SUPERCAP: "supercapital"}

_HULL_CATEGORY = {
    "Titan": SUPERCAP,
    "Supercarrier": SUPERCAP,
    "Carrier": CAPITAL,
    "Dreadnought": CAPITAL,
    "Force Auxiliary": CAPITAL,
    "Capital Industrial": CAPITAL,  # Rorqual
    "Jump Freighter": SUBCAP,       # freighters dock as subcaps -> anywhere
    "Black Ops": SUBCAP,
}

# Upwell structure typeID -> highest category it will let dock.
STRUCTURE_DOCK_CATEGORY = {
    35832: SUBCAP,   # Astrahus (M citadel)
    35833: CAPITAL,  # Fortizar (L citadel)
    35834: SUPERCAP, # Keepstar (XL citadel)
    35825: SUBCAP,   # Raitaru (M engineering)
    35826: SUBCAP,   # Azbel (L engineering) - builds caps but docks subcaps only
    35827: CAPITAL,  # Sotiyo (XL engineering)
    35835: SUBCAP,   # Athanor (M refinery)
    35836: CAPITAL,  # Tatara (XL refinery)
    # Faction Fortizars (capital docking):
    47512: CAPITAL,  # 'Horizon' Fortizar
    47513: CAPITAL,  # 'Marginis' Fortizar
    47514: CAPITAL,  # 'Prometheus' Fortizar
    47515: CAPITAL,  # 'Moreau' Fortizar
    47516: CAPITAL,  # 'Draccous' Fortizar
    40340: SUPERCAP, # Upwell Palatine Keepstar
}
# Structures with no docking at all (jump gates, cyno beacons/jammers, drills).
STRUCTURE_NO_DOCK = {35840, 35841, 35836 - 1, 37534, 81826}  # best-effort; editable

# NPC station *type models* that are kickout (unsafe undock). Matched by the
# station type name (from invTypes) since typeIDs vary. Source: Jambe's guide.
KICKOUT_STATION_TYPE_NAMES = {
    "amarr industrial station",
    "minmatar research station",
    "minmatar station",
    "gallente industrial station",
    "sisters of eve industrial station",
    "minmatar hub",
    "minmatar trade post",
}

# Known public "freeport" structures (dockable but risky). Matched by a
# case-insensitive substring of the structure name, or by structure_id.
FREEPORT_NAME_HINTS = {
    "summit's beacon",   # Turnur - Summit's Beacon (Oblivion Watch)
}
FREEPORT_STRUCTURE_IDS: set[int] = set()

# A battleship is ~0.5e6 m3; capitals are far larger. Stations whose
# max_dockable_ship_volume is below this are treated as subcap-only.
CAPITAL_DOCK_VOLUME = 1_000_000.0


def ship_category(ship: Ship) -> int:
    return _HULL_CATEGORY.get(ship.hull_class, SUBCAP)


def can_use_highsec_gates(ship: Ship) -> bool:
    """Only subcap-hulls (jump freighters, freighters, Black Ops) may take
    high-sec gates. Capitals/supers can never enter high-sec."""
    return ship_category(ship) == SUBCAP


def gate_allowed(ship: Ship, security: float) -> bool:
    """May this hull traverse a gate INTO a system of the given security?"""
    from ..config import JUMPABLE_SECURITY_MAX
    if can_use_highsec_gates(ship):
        return True
    return security < JUMPABLE_SECURITY_MAX


@dataclass
class DockCheck:
    can_dock: bool
    safe: bool
    note: str

    @property
    def status(self) -> str:
        if not self.can_dock:
            return "no docking"
        if not self.safe:
            return "risky"
        return "ok"


def check_structure(ship: Ship, type_id: int, name: str = "",
                    structure_id: int = 0) -> DockCheck:
    cat = ship_category(ship)
    if type_id in STRUCTURE_NO_DOCK:
        return DockCheck(False, False, "structure has no docking")
    allowed = STRUCTURE_DOCK_CATEGORY.get(type_id)
    lname = name.lower()
    freeport = (structure_id in FREEPORT_STRUCTURE_IDS
                or any(h in lname for h in FREEPORT_NAME_HINTS))
    if allowed is None:
        # Unknown structure type: allow but warn.
        note = "unknown structure type - verify docking"
        if freeport:
            note = "freeport (public) - risk: access can be revoked"
        return DockCheck(True, not freeport, note)
    can = cat <= allowed
    if not can:
        return DockCheck(False, False,
                         f"{_CAT_NAME[cat]} cannot dock (max {_CAT_NAME[allowed]})")
    if freeport:
        return DockCheck(True, False, "freeport (public) - risk: access can be revoked")
    return DockCheck(True, True, "docking ok")


def check_npc_station(ship: Ship, type_name: str = "",
                      max_volume: float = 0.0) -> DockCheck:
    cat = ship_category(ship)
    if cat == SUPERCAP:
        return DockCheck(False, False, "supercapitals cannot dock at NPC stations")
    if cat == CAPITAL and max_volume and max_volume < CAPITAL_DOCK_VOLUME:
        return DockCheck(False, False, "station too small for capitals")
    kickout = type_name.strip().lower() in KICKOUT_STATION_TYPE_NAMES
    if kickout:
        return DockCheck(True, False, "kickout station - unsafe undock (no docking ring)")
    return DockCheck(True, True, "docking ring - safe")
