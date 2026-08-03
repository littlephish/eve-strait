"""Shared UI data types: a waypoint and the docks available in its system."""
from __future__ import annotations

from dataclasses import dataclass

from ..data import docking
from ..data.ships import Ship
from ..data.universe import System, Universe


@dataclass
class DockOption:
    name: str
    type_id: int
    kind: str        # "station" or "structure"
    can_dock: bool
    safe: bool
    note: str
    owner_id: int = 0

    def key(self) -> tuple:
        return (self.kind, self.type_id, self.name)


@dataclass
class Waypoint:
    system: System
    # Explicit user dock choice; None means "auto-pick the best".
    chosen: DockOption | None = None


def docks_for_system(universe: Universe, dockables: list, ship: Ship,
                     system_id: int, standings: dict | None = None,
                     hostile_threshold: float = 0.0,
                     exclude_hostile: bool = False) -> list[DockOption]:
    """All docks in a system (NPC stations + known player structures),
    annotated with whether ``ship`` can use them. When ``exclude_hostile`` is
    set, player structures owned by an entity with standing below
    ``hostile_threshold`` are marked unsafe."""
    opts: list[DockOption] = []
    for st in universe.system_stations.get(system_id, []):
        chk = docking.check_npc_station(ship, st.type_name, st.max_volume)
        opts.append(DockOption(st.name, st.type_id, "station",
                               chk.can_dock, chk.safe, chk.note))
    for d in dockables:
        if getattr(d, "solar_system_id", 0) == system_id and d.kind == "structure":
            chk = docking.check_structure(ship, d.type_id, d.name, d.location_id)
            safe, note = chk.safe, chk.note
            owner = getattr(d, "owner_id", 0)
            if (exclude_hostile and standings and owner in standings
                    and standings[owner] < hostile_threshold):
                safe = False
                note = f"hostile owner (standing {standings[owner]:+.1f})"
            opts.append(DockOption(d.name, d.type_id, "structure",
                                   chk.can_dock, safe, note, owner_id=owner))
    # Usable & safe first, then usable, then the rest.
    opts.sort(key=lambda o: (not o.can_dock, not o.safe, o.name))
    return opts


def best_dock(opts: list[DockOption]) -> DockOption | None:
    for o in opts:
        if o.can_dock and o.safe:
            return o
    for o in opts:
        if o.can_dock:
            return o
    return None


def effective_dock(wp: Waypoint, opts: list[DockOption]) -> DockOption | None:
    """The dock to display/use: the user's choice if still present, else auto."""
    if wp.chosen is not None:
        for o in opts:
            if o.key() == wp.chosen.key():
                return o
    return best_dock(opts)
