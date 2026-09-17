"""Which of your characters is sitting in a ship with a cyno fitted, and where.

ESI publishes nothing about cynos as such -- there is no endpoint for them and
no way to see anyone else's. What it does publish is generic: the ship a
character is currently in, and that character's assets. Fitted modules appear
in the asset list with their ``location_id`` set to the ship's ``item_id`` and
a ``location_flag`` naming the slot, so a cyno on your alt is findable by
joining those two together. That is the whole trick, and it only ever works for
characters you have personally linked.

The honest limits, which the UI repeats rather than hides:

* **Assets lag.** ESI caches the asset list for about an hour, so a cyno fitted
  in the last few minutes may not be visible yet, and one just unfitted may
  still show. Location and ship are near-live, so the *where* is current even
  when the *what* is not.
* **Docked characters still count.** A cyno in a hangar is not lit, and this
  cannot tell you whether one is actually up -- only that the character is in a
  ship that could light one.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, fields

# Fitted modules report the slot they sit in. Only high slots can hold a cyno,
# and checking the flag stops a spare in the cargo hold from counting as fitted.
HIGH_SLOT_PREFIX = "HiSlot"


@dataclass(frozen=True)
class CynoAlt:
    """A linked character parked in a cyno-capable ship."""
    character_id: int
    character_name: str
    system_id: int
    ship_type_id: int
    ship_name: str
    module_type_id: int
    module_name: str
    docked: bool = False

    @property
    def covert(self) -> bool:
        return "Covert" in self.module_name

    @property
    def industrial(self) -> bool:
        return "Industrial" in self.module_name

    def summary(self) -> str:
        kind = ("covert" if self.covert
                else "industrial" if self.industrial else "standard")
        where = "docked" if self.docked else "in space"
        return f"{self.ship_name} - {kind} cyno, {where}"


def fitted_cyno(assets, ship_item_id: int,
                cyno_modules: dict[int, str]) -> tuple[int, str] | None:
    """The cyno fitted to that ship's high slots, if there is one.

    ``assets`` is the raw ESI asset list. ``cyno_modules`` is typeID -> name
    for the fittable generators, which the SDE gives us by module group.
    """
    if not ship_item_id:
        return None
    for item in assets:
        if item.get("location_id") != ship_item_id:
            continue
        flag = str(item.get("location_flag") or "")
        if not flag.startswith(HIGH_SLOT_PREFIX):
            continue
        type_id = item.get("type_id")
        if type_id in cyno_modules:
            return type_id, cyno_modules[type_id]
    return None


def alt_from(character_id: int, character_name: str, location: dict,
             ship: dict, assets, cyno_modules: dict[int, str]) -> CynoAlt | None:
    """Build a CynoAlt from one character's ESI payloads, or None.

    Every argument is the plain decoded JSON so this stays testable without a
    network, which matters more here than usual: the join between assets and
    the active ship is the part that is easy to get subtly wrong.
    """
    system_id = (location or {}).get("solar_system_id")
    if not system_id:
        return None
    hit = fitted_cyno(assets or [], (ship or {}).get("ship_item_id") or 0,
                      cyno_modules)
    if hit is None:
        return None
    module_type_id, module_name = hit
    return CynoAlt(
        character_id=character_id,
        character_name=character_name,
        system_id=int(system_id),
        ship_type_id=int((ship or {}).get("ship_type_id") or 0),
        ship_name=str((ship or {}).get("ship_name") or "").strip() or "ship",
        module_type_id=module_type_id,
        module_name=module_name,
        # A station or structure id means parked rather than sitting on grid.
        docked=bool((location or {}).get("station_id")
                    or (location or {}).get("structure_id")),
    )


def systems_with_cyno(alts) -> set[int]:
    """The systems you can currently jump to, as far as this can tell."""
    return {a.system_id for a in alts}


# -- remembering between launches -------------------------------------------
# A scan costs a full asset read per linked character, and the answer to "do I
# have a way home" should survive closing the app. Cached beside the other
# per-character caches, under %LOCALAPPDATA%.
def _store_path():
    from .. import config

    return config.CACHE_DIR / "cyno_alts.json"


def save_alts(alts, notes=None, path=None, now: float | None = None) -> None:
    """Remember the last roll-call. Failure is silent and harmless."""
    path = path or _store_path()
    payload = {"fetched": time.time() if now is None else now,
               "notes": list(notes or []),
               "alts": [asdict(a) for a in alts or ()]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass            # a cache that cannot be written is not an error


def load_alts(path=None) -> tuple[list, list, float | None]:
    """The remembered roll-call as (alts, notes, fetched_epoch).

    Returns empty on anything unreadable. A half-written cache should cost
    the feature, never the launch.

    Unknown keys are ignored and incomplete entries dropped, so a cache
    written by a newer build degrades rather than crashing an older one.
    """
    path = path or _store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], [], None
    if not isinstance(data, dict):
        return [], [], None
    known = {f.name for f in fields(CynoAlt)}
    required = {f.name for f in fields(CynoAlt)
                if f.default is not False and f.name != "docked"}
    alts = []
    for row in data.get("alts") or ():
        if not isinstance(row, dict):
            continue
        kept = {k: v for k, v in row.items() if k in known}
        if not required <= set(kept):
            continue        # older or partial record: drop the entry, not all
        try:
            alts.append(CynoAlt(**kept))
        except TypeError:
            continue
    fetched = data.get("fetched")
    return alts, list(data.get("notes") or []), (
        float(fetched) if isinstance(fetched, (int, float)) else None)


def describe_age(fetched: float | None, now: float | None = None) -> str:
    """How old a remembered roll-call is, in words.

    Location and ship are near-live *while the app is running*. Once it has
    closed, even those are a snapshot -- the alt may have moved, docked or
    logged off. So a restored list is always labelled with its age rather
    than shown as current.
    """
    if not fetched:
        return "never scanned"
    seconds = (time.time() if now is None else now) - fetched
    if seconds < 60:
        return "just now"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{int(round(minutes))} minutes ago"
    hours = minutes / 60.0
    if hours < 48:
        return f"{hours:.1f} hours ago"
    return f"{hours / 24:.1f} days ago"
