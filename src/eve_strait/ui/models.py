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
    standing: float | None = None   # toward the owner (structures only)
    relation: str = ""              # "your corporation" / "your alliance" / ...
    has_rights: bool = False        # configured docking rights with the owner
    can_tether: bool = False        # too big to dock, but can tether here

    @property
    def is_own(self) -> bool:
        return self.relation in ("your corporation", "your alliance")

    def key(self) -> tuple:
        return (self.kind, self.type_id, self.name)

    @property
    def sort_key(self) -> tuple:
        """Rank, then prefer non-negative owners, then name. Keeps a red-but-
        permitted structure below a neutral one of the same rank."""
        hostile = 1 if (self.standing is not None and self.standing < 0) else 0
        return (self.rank, hostile, self.name)

    @property
    def rank(self) -> int:
        """Preference order (lower is better):
        0 your corp's structure, 1 your alliance's, 2 explicit docking rights,
        3 positive-standing structure, 4 safe NPC station, 5 kickout NPC
        station, 6 neutral/unknown structure, 7 hostile, 9 undockable.

        Configured docking rights outrank a merely-blue standing: access is a
        permission, and plenty of neutral (or worse) entities grant it.
        """
        if not self.can_dock:
            # A capital that cannot dock can still tether, which beats sitting
            # in open space; better than nothing, worse than any real dock.
            return 8 if self.can_tether else 9
        if self.kind == "structure":
            if self.relation == "your corporation":
                return 0
            if self.relation == "your alliance":
                return 1
            if self.has_rights:
                return 2
            if self.standing is not None and self.standing > 0:
                return 3
            if self.standing is not None and self.standing < 0:
                return 7
            return 6
        return 4 if self.safe else 5


@dataclass
class Waypoint:
    system: System
    # Explicit user dock choice; None means "auto-pick the best".
    chosen: DockOption | None = None


def docks_for_system(universe: Universe, dockables: list, ship: Ship,
                     system_id: int, standings: dict | None = None,
                     hostile_threshold: float = 0.0,
                     exclude_hostile: bool = False,
                     relation=None, has_rights=None,
                     starbases: int = 0) -> list[DockOption]:
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
            chk = docking.check_structure_tether(ship, d.type_id, d.name,
                                                 d.location_id)
            tether = not chk.can_dock and docking.can_tether_at(d.type_id)
            safe, note = chk.safe, chk.note
            owner = getattr(d, "owner_id", 0)
            # Resolve how we relate to the owner (own corp/alliance, contact...).
            standing, label = None, ""
            if relation is not None:
                standing, label = relation(owner)
            elif standings and owner in standings:
                standing = standings[owner]
            rights = bool(has_rights and has_rights(owner))
            if standing is not None and label:
                note = f"{note} · {label} ({standing:+.1f})"
            if rights:
                # An explicit permission beats an inferred standing: keep it
                # usable even if the owner is red.
                safe = True
                note = f"{note} · docking rights"
            elif (exclude_hostile and standing is not None
                    and standing < hostile_threshold):
                safe = False
                note = f"hostile owner (standing {standing:+.1f})"
            opts.append(DockOption(d.name, d.type_id, "structure",
                                   chk.can_dock, safe, note, owner_id=owner,
                                   standing=standing, relation=label,
                                   has_rights=rights, can_tether=tether))
    if starbases:
        # A corp POS shield: not docking, but a capital can sit safely inside.
        opts.append(DockOption(
            f"Corp POS ({starbases} tower{'s' if starbases > 1 else ''})",
            0, "starbase", False, True, "POS shield - safe park, no docking",
            can_tether=True))
    # Preference: own structures > friendly structures > safe NPC stations >
    # kickout stations > neutral structures > hostile > tether-only.
    opts.sort(key=lambda o: o.sort_key)
    return opts


# Well-known preferred docks when a system has many and the user hasn't pinned
# one. Matched as a case-insensitive substring of the station name.
PREFERRED_DOCK_HINTS = {
    30000142: "Jita 4 - Moon 4 - Caldari Navy Assembly Plant",  # Jita 4-4
}


def best_dock(opts: list[DockOption], system_id: int = 0,
              pinned: str | None = None) -> DockOption | None:
    """Pick a dock: the user's pinned default, then a well-known preferred
    station (e.g. Jita 4-4), then the first safe/usable one."""
    usable = [o for o in opts if o.can_dock]
    if pinned:
        for o in usable or opts:
            if o.name == pinned:
                return o
    hint = PREFERRED_DOCK_HINTS.get(system_id)
    if hint:
        low = hint.lower()
        for o in usable:
            if o.name.lower() == low or low in o.name.lower():
                return o
    # Otherwise take the best-ranked dock (own > friendly > safe NPC > ...).
    return min(usable, key=lambda o: o.sort_key) if usable else None


def effective_dock(wp: Waypoint, opts: list[DockOption],
                   pinned: str | None = None) -> DockOption | None:
    """The dock to display/use: the user's per-waypoint choice if still
    present, else the pinned/preferred/auto pick."""
    if wp.chosen is not None:
        for o in opts:
            if o.key() == wp.chosen.key():
                return o
    return best_dock(opts, wp.system.id, pinned)
