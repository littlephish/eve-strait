"""Wanderer map integration: route over your corp's own scanned chains.

Wanderer (wanderer.ltd) is a self-hosted wormhole mapper. Where EVE-Scout
publishes the two public hubs, a Wanderer map is whatever your group has
actually scanned, which is usually far more useful and always more current.

Because it is self-hosted there is no single endpoint to hard-code: the user
supplies their instance URL, a map slug or UUID, and that map's API token.

    GET {base}/api/maps/{map}/systems
    GET {base}/api/maps/{map}/connections
    Authorization: Bearer <map token>

**J-space chains are collapsed.** A Wanderer map is mostly wormhole systems,
and this app only knows k-space, so taking the connections at face value would
throw nearly all of them away -- the interesting ones especially, since a chain
usually runs k-space -> J -> J -> k-space. Instead each k-space end is walked
outward through J-space to whatever k-space it reaches, and that whole path
becomes one edge costing the hops it really takes. Same collapse
``evescout.graph`` does for Thera, generalised to arbitrary depth.

Mass limits come from the wormhole type code where there is one, reusing the
table in evescout for exactly the reason documented there: the size label
cannot be trusted and the type code can.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

from .. import config
from . import evescout

_UA = "eve-strait/0.1 (+https://github.com/littlephish/eve-strait)"
_STORE = config.CACHE_DIR / "wanderer.json"

# Chains get long and every extra hop is another chance the far end has already
# collapsed. Past this it is not a shortcut any more.
MAX_HOPS = 6

# Wanderer records a hole's remaining life as a status rather than hours.
TIME_STATUS = {0: "fresh", 1: "end of life"}
MASS_STATUS = {0: "fresh", 1: "reduced", 2: "critical"}

# ship_size_type is documented only as "1 = frigate-sized", so anything else is
# read as unconstrained and the wormhole type decides. A frigate hole is capped
# hard, because sending a freighter at one is the expensive mistake.
FRIGATE_MAX_T = 5_000


def _get(base_url: str, token: str, path: str, timeout: float = 20.0):
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": _UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch(base_url: str, token: str, map_id: str, timeout: float = 20.0) -> dict:
    """Pull one map's systems and connections. Raises on failure."""
    quoted = urllib.parse.quote(map_id, safe="")
    systems = _get(base_url, token, f"/api/maps/{quoted}/systems", timeout)
    conns = _get(base_url, token, f"/api/maps/{quoted}/connections", timeout)
    # The two endpoints disagree on nesting: systems come back under
    # data.systems, connections directly under data.
    data = systems.get("data") if isinstance(systems, dict) else None
    rows = (data or {}).get("systems") if isinstance(data, dict) else data
    return {
        "systems": rows or [],
        "connections": (conns.get("data") if isinstance(conns, dict) else conns) or [],
        "fetched": time.time(),
    }


def _max_t(conn: dict) -> int:
    """Heaviest single ship this connection will pass."""
    if conn.get("ship_size_type") == 1:
        return FRIGATE_MAX_T
    wh_type = (conn.get("wormhole_type") or "").upper()
    # K162 is the generic exit signature and says nothing about size; the far
    # side carries the real code. Fall through to the conservative default.
    if wh_type and wh_type != "K162":
        known = evescout.WH_MAX_JUMP_T.get(wh_type)
        if known:
            return known
    # Unknown type: assume the smallest thing it could be rather than route a
    # freighter into a hole it bounces off.
    return 62_000


def edges(data: dict, systems) -> dict:
    """Collapse the map into k-space edges the router can use.

    ``systems`` is Universe.systems; membership in it is the k-space test,
    since J-space is never loaded.

    Returns {(a, b): info} in the same shape evescout.graph produces, so both
    sources can be merged and the router does not care where an edge came from.
    """
    adjacency: dict[int, list[dict]] = {}
    for c in data.get("connections") or ():
        a, b = c.get("solar_system_source"), c.get("solar_system_target")
        if not a or not b or a == b:
            continue
        adjacency.setdefault(a, []).append({"to": b, "conn": c})
        adjacency.setdefault(b, []).append({"to": a, "conn": c})

    out: dict[tuple[int, int], dict] = {}

    def add(a, b, info):
        key = (a, b) if a < b else (b, a)
        old = out.get(key)
        # Prefer the roomier way through, then the shorter one: a chain that
        # takes a freighter beats a shorter one that does not.
        if (old is None or info["max_t"] > old["max_t"]
                or (info["max_t"] == old["max_t"] and info["hops"] < old["hops"])):
            out[key] = info

    for start in adjacency:
        if start not in systems:
            continue                    # only walk outward from k-space
        # BFS through J-space only. Reaching k-space ends that branch: the
        # router can plan onward from there itself.
        queue = deque([(start, 0, [], None)])
        seen = {start}
        while queue:
            node, hops, types, worst = queue.popleft()
            if hops >= MAX_HOPS:
                continue
            for link in adjacency.get(node, ()):
                nxt, conn = link["to"], link["conn"]
                if nxt in seen:
                    continue
                cap = _max_t(conn)
                limit = cap if worst is None else min(worst, cap)
                kinds = types + [conn.get("wormhole_type") or "?"]
                if nxt in systems:
                    if nxt != start:
                        add(start, nxt, {
                            "via": "Wanderer", "hops": hops + 1,
                            "size": "unknown", "wh_types": kinds,
                            "max_t": limit, "sigs": {},
                            "hours": None,
                            "mass": MASS_STATUS.get(conn.get("mass_status")),
                            "eol": conn.get("time_status") == 1,
                        })
                    continue            # do not route *through* other k-space
                seen.add(nxt)
                queue.append((nxt, hops + 1, kinds, limit))
    return out


# -- cache ------------------------------------------------------------------
def load() -> dict:
    try:
        return json.loads(_STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    try:
        _STORE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def refresh(timeout: float = 20.0) -> dict:
    """Fetch using the stored settings. Returns {} when not configured."""
    base, token, map_id = (config.get_wanderer_url(), config.get_wanderer_token(),
                           config.get_wanderer_map())
    if not (base and token and map_id):
        return {}
    data = fetch(base, token, map_id, timeout)
    save(data)
    return data


def describe(data: dict) -> str:
    """One line for the UI about what came back."""
    if not data:
        return "Wanderer is not configured."
    conns = data.get("connections") or []
    frig = sum(1 for c in conns if c.get("ship_size_type") == 1)
    eol = sum(1 for c in conns if c.get("time_status") == 1)
    bits = [f"{len(conns)} connections", f"{len(data.get('systems') or [])} systems"]
    if frig:
        bits.append(f"{frig} frigate-only")
    if eol:
        bits.append(f"{eol} end of life")
    return "Wanderer: " + ", ".join(bits)
