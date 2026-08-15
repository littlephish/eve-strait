"""Public Thera and Turnur wormhole connections, from EVE-Scout.

EVE-Scout scouts and publishes the wormholes joining the two public hubs --
Thera and Turnur -- to the rest of New Eden. They are the only wormholes worth
routing over, because they are the only ones somebody else has already found
and written down. Everything here is other people's volunteer scanning: a
connection can be gone before the route is flown.

Two things about the data decide how it is used.

**Half of it is not routable.** A signature's far side is often another
wormhole system (J-space), which this app does not load and cannot route
through. Those are dropped; only connections landing in k-space survive.

**Thera is not a system this app knows.** J-space is filtered out when the SDE
is parsed, so Thera has no id in the universe and cannot be a node in a route.
Turnur is ordinary low-sec and needs no special handling. See ``graph`` for how
the two are reconciled.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .. import config

_API = "https://api.eve-scout.com/v2/public/signatures"
_UA = "eve-strait/0.1 (+https://github.com/littlephish/eve-strait)"

# Wormhole size classes in ascending order, as EVE-Scout reports them.
SIZES = ("small", "medium", "large", "xlarge", "capital")
_SIZE_RANK = {s: i for i, s in enumerate(SIZES)}

# Maximum mass ONE ship may have to jump a hole, in tonnes, by wormhole type.
# From the EVE University wormhole attribute table.
#
# This exists because ``max_ship_size`` cannot be trusted. EVE-Scout labels
# M164 and V898 "xlarge" though both cap a single jump at 375,000 t, the same
# as F135 and N968 which it labels "large". A jump freighter is 960,000 t, so
# believing the label routes it into a hole it physically bounces off -- five
# of twenty-eight live signatures, the day this was written. The type code is
# unambiguous, so the type code decides.
WH_MAX_JUMP_T = {
    # 62,000 t -- cruisers and below
    "F353": 62_000, "J377": 62_000, "Q063": 62_000, "T458": 62_000,
    # 375,000 t -- battleships; freighters and capitals bounce
    "F135": 375_000, "M164": 375_000, "N968": 375_000, "V898": 375_000,
    # 1,000,000 t -- freighters and the Rorqual, but no carrier or dread
    "B449": 1_000_000, "E587": 1_000_000, "L031": 1_000_000,
    # 2,000,000 t -- capitals
    "N944": 2_000_000, "S199": 2_000_000,
}

# Only for a hole whose type is not in the table above -- a K162 nobody has
# jumped yet, or a type added since. The label is all there is then, so take
# the smallest mass it can mean and accept being too strict rather than
# sending a freighter at a hole that will not take it.
_LABEL_MIN_T = {"small": 5_000, "medium": 62_000, "large": 375_000,
                "xlarge": 375_000, "capital": 2_000_000}

# Heaviest hull in each class, tonnes, from ESI type mass. Heaviest rather than
# per-ship so the answer never depends on which of four near-identical hulls is
# selected, and so it errs toward refusing a hole rather than over-promising.
MASS_BY_HULL_T = {
    "Subcapital": 62_000,           # stand-in: the generic gate-only planner
    "Black Ops": 151_100,           # Widow
    "Jump Freighter": 960_000,      # Rhea
    "Capital Industrial": 800_000,  # Rorqual -- lighter than a carrier
    "Carrier": 1_260_000,           # Archon
    "Dreadnought": 1_290_000,       # Revelation
    "Force Auxiliary": 1_310_000,   # Apostle
    "Supercarrier": 1_780_000,      # Aeon -- does fit a 2,000,000 t hole
    "Titan": 2_400_000,             # Avatar -- fits nothing
}

_STORE = config.CACHE_DIR / "evescout.json"
# Connections last hours and new ones are scanned constantly, so this is stale
# quickly. Short enough to be worth re-fetching on a route, long enough not to
# hammer a volunteer-run service.
CACHE_TTL = 15 * 60


def max_jump_t(wh_type: str | None, size: str | None) -> int:
    """What one ship may weigh to pass this hole, in tonnes.

    The type code decides where it is known, because it is exact; the reported
    size is only a fallback. 0 means nothing may pass, which is what an
    unrecognised size gets -- guessing generously here is how a capital ends
    up routed through a hole it cannot enter.
    """
    by_type = WH_MAX_JUMP_T.get(wh_type or "")
    if by_type:
        return by_type
    return _LABEL_MIN_T.get(size or "", 0)


def fits(hull_class: str, hole) -> bool:
    """Can this hull pass this hole?

    ``hole`` is an edge from graph() (which carries a resolved ``max_t``), a
    raw connection (``wh_type`` and ``size``), or a bare size string for a
    caller that has nothing better.
    """
    mass = MASS_BY_HULL_T.get(hull_class)
    if mass is None:
        return False                # unclassified hull: refuse, do not guess
    if isinstance(hole, str):
        limit = max_jump_t(None, hole)
    elif "max_t" in hole:
        limit = hole["max_t"]
    else:
        limit = max_jump_t(hole.get("wh_type"), hole.get("size"))
    return 0 < mass <= limit


def fetch(timeout: float = 30.0) -> list[dict]:
    """Raw signature list from EVE-Scout. Raises on network or parse failure."""
    req = urllib.request.Request(_API, headers={"User-Agent": _UA,
                                                "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def connections(rows, systems) -> list[dict]:
    """Keep the signatures that are actually routable.

    ``systems`` is Universe.systems, used to test whether the far side is a
    system this app knows -- which is exactly the k-space test, since J-space
    is never loaded.

    Each result is {hub, system_id, hub_sig, far_sig, size, hours}. ``hub_sig``
    is the signature as seen from Thera or Turnur, ``far_sig`` as seen from the
    k-space end, so whichever side you are standing on there is an id to
    search for.
    """
    out = []
    for r in rows or ():
        if r.get("signature_type") != "wormhole":
            continue
        hub = r.get("out_system_name")
        far = r.get("in_system_id")
        if hub not in ("Thera", "Turnur") or far not in systems:
            continue
        out.append({
            "hub": hub,
            "system_id": far,
            "hub_sig": r.get("out_signature") or "?",
            "far_sig": r.get("in_signature") or "?",
            "size": r.get("max_ship_size") or "unknown",
            "wh_type": r.get("wh_type"),
            "hours": r.get("remaining_hours"),
            # When a volunteer first put this signature in, and when anyone
            # last touched it. Remaining hours says how long the hole has left;
            # these say how much to trust that number, which is a different
            # question and the one you want before committing a capital.
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        })
    return out


def age_hours(stamp: str | None) -> float | None:
    """Hours since an EVE-Scout timestamp, or None if it cannot be read."""
    if not stamp:
        return None
    import datetime as _dt

    text = str(stamp).strip().replace("Z", "+00:00")
    try:
        when = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.UTC)
    return (_dt.datetime.now(_dt.UTC) - when).total_seconds() / 3600.0


def describe_age(stamp: str | None) -> str:
    """"14 minutes ago" / "3.2 hours ago", or a plain unknown."""
    hours = age_hours(stamp)
    if hours is None:
        return "unknown"
    if hours < 0:
        return "just now"
    if hours < 1:
        return f"{int(round(hours * 60))} minutes ago"
    if hours < 48:
        return f"{hours:.1f} hours ago"
    return f"{hours / 24:.1f} days ago"


def hulls_that_fit(max_t: int) -> list[str]:
    """Which hull classes can pass a hole of this per-jump mass limit."""
    return [name for name, mass in sorted(MASS_BY_HULL_T.items(),
                                          key=lambda kv: kv[1])
            if max_t and mass <= max_t]


def graph(conns, turnur_id: int | None):
    """Turn connections into routable edges: {(a, b): info}, a < b.

    Turnur is a k-space system, so each of its connections is one edge, one
    jump, exactly like an Ansiblex.

    Thera is not in the universe at all, so it cannot be a waypoint. A trip
    through it is instead collapsed into a single edge between the two k-space
    systems either end -- in one hole, out the other -- costing the two jumps
    it really takes. Every pair of Thera connections gives one such edge, which
    is quadratic but tiny: a dozen or so scouted holes is under a hundred
    edges. The alternative, giving Thera a node, means inventing coordinates
    for it in a k-space map and then defending every distance calculation from
    them.

    Every edge carries ``max_t``, the heaviest single ship that can use it. A
    collapsed Thera edge takes the smaller of its two holes, since the ship has
    to pass through both.
    """
    edges: dict[tuple[int, int], dict] = {}

    def add(a, b, info):
        if a == b:
            return
        key = (a, b) if a < b else (b, a)
        # Keep the roomiest way through: two holes may join the same pair.
        old = edges.get(key)
        if old is None or info["max_t"] > old["max_t"]:
            edges[key] = info

    thera = [c for c in conns if c["hub"] == "Thera"]
    for c in conns:
        if c["hub"] == "Turnur" and turnur_id is not None:
            add(turnur_id, c["system_id"], {
                "via": "Turnur", "hops": 1, "size": c["size"],
                "wh_types": [c["wh_type"]],
                "max_t": max_jump_t(c["wh_type"], c["size"]),
                "sigs": {turnur_id: c["hub_sig"], c["system_id"]: c["far_sig"]},
                "hours": c["hours"]})

    for i, a in enumerate(thera):
        for b in thera[i + 1:]:
            size = min(a["size"], b["size"],
                       key=lambda s: _SIZE_RANK.get(s, -1))
            hours = [h for h in (a["hours"], b["hours"]) if h is not None]
            add(a["system_id"], b["system_id"], {
                "via": "Thera", "hops": 2, "size": size,
                "wh_types": [a["wh_type"], b["wh_type"]],
                "max_t": min(max_jump_t(a["wh_type"], a["size"]),
                             max_jump_t(b["wh_type"], b["size"])),
                "sigs": {a["system_id"]: a["far_sig"],
                         b["system_id"]: b["far_sig"]},
                "hours": min(hours) if hours else None})
    return edges


# -- disk cache -------------------------------------------------------------
def load(max_age: float = CACHE_TTL) -> dict:
    """Cached signatures, or {} if there are none worth using.

    Returns {"rows": [...], "fetched": epoch}. Callers get the age too, so a
    stale list can still be shown with a warning rather than silently used as
    though it were current -- these connections expire in hours.
    """
    try:
        data = json.loads(_STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or "rows" not in data:
        return {}
    if max_age and time.time() - data.get("fetched", 0) > max_age:
        data["stale"] = True
    return data


def save(rows) -> None:
    try:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        _STORE.write_text(json.dumps({"rows": rows, "fetched": time.time()}),
                          encoding="utf-8")
    except OSError:
        pass


def refresh(timeout: float = 30.0) -> dict:
    """Fetch and cache. Returns the same shape as load(); {} on failure."""
    try:
        rows = fetch(timeout=timeout)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return load(max_age=0) or {}
    save(rows)
    return {"rows": rows, "fetched": time.time()}
