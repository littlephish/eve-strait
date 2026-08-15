"""The tools the assistant can call, defined once for every provider.

Anthropic and OpenAI describe tools with slightly different JSON, but the
*names, descriptions and schemas* must not drift between them or the assistant
behaves differently depending on which engine is selected. So they live here
once and each provider adapts the shape at the edge.

Descriptions are deliberately prescriptive about **when** to call a tool, not
just what it does. That is what drives a model to reach for the right one.

Every function takes the MainWindow as ``app`` and is called from the UI
thread via a queued call, because it mutates panels. Almost nothing here
touches Qt directly -- auto_route is the one exception, and it says why.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from .. import config


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict            # JSON Schema for the arguments
    fn: Callable            # fn(app, **kwargs) -> str
    writes: bool = False    # mutates the route, settings or the game client


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props,
            "required": required or [], "additionalProperties": False}


_SYSTEM = {"type": "string", "description": "Solar system name, e.g. Jita."}


# -- helpers ----------------------------------------------------------------
def _resolve(app, name: str):
    """Name -> System, tolerating case and partial matches."""
    uni = app.universe
    if uni is None:
        raise ValueError("Map data is still loading. Try again shortly.")
    s = uni.by_name(name)
    if s is not None:
        return s
    hits = [x for x in uni.systems.values() if x.name.lower() == name.lower()]
    if not hits:
        hits = [x for x in uni.systems.values()
                if x.name.lower().startswith(name.lower())]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"No system named {name!r}.")
    raise ValueError(f"{name!r} is ambiguous: {', '.join(h.name for h in hits[:8])}")


def _describe(app, s) -> dict:
    uni = app.universe
    sov = app.sov_of(s.id)
    return {
        "name": s.name,
        "security": round(s.security, 2),
        "class": ("high-sec" if s.security >= 0.5 else
                  "low-sec" if s.security > 0.0 else "null-sec"),
        "region": uni.region_names.get(s.region_id, str(s.region_id)),
        "can_jump_into": s.jumpable,
        "sovereignty": sov[0] if sov else None,
        "avoided": app.is_avoided(s.id),
        "your_note": app.note_for(s.id) or None,
    }


# -- read tools -------------------------------------------------------------
def find_system(app, name: str) -> str:
    return json.dumps(_describe(app, _resolve(app, name)))


def system_intel(app, name: str) -> str:
    """Activity signals. See the honesty notes baked into the payload."""
    s = _resolve(app, name)
    intel = app.system_intel(s.id)
    out = _describe(app, s)
    out.update({
        "gate_traffic_1h": intel["jumps_1h"],
        "gate_traffic_24h": intel["jumps_24h"],
        "accumulated_hours": intel["history_hours"],
        "npc_kills_1h": intel["npc_kills_1h"],
        "ship_kills_1h": intel["ship_kills_1h"],
        "pod_kills_1h": intel["pod_kills_1h"],
        "adm": intel["adm"],
        "industry_index": (intel.get("industry") or {}).get("manufacturing"),
        "_caveats": (
            "ESI publishes no per-system player count. gate_traffic is the "
            "closest proxy for how busy a system is. ADM (1 to 6) rises with "
            "sustained ratting, mining and industry, so it is the best signal "
            "for whether anyone lives there. Kill and traffic figures cover "
            "the last full hour only; the 24h figures are accumulated locally "
            "and are partial until the app has run a day."),
    })
    return json.dumps(out)


def cyno_activity(app, name: str) -> str:
    from ..esi import zkill
    s = _resolve(app, name)
    result = zkill.cyno_losses(s.id)
    return json.dumps({"system": s.name, "summary": zkill.describe(result),
                       "losses": result.get("losses", 0),
                       "_caveat": "Counts ships that DIED with a cyno fitted. "
                                  "A floor on cyno traffic, never a count."})


def get_route(app) -> str:
    """Waypoints plus the current plan, so the model can explain or fix it."""
    from ..jump import router
    wps = app.route.systems()
    if not wps:
        return json.dumps({"waypoints": [], "note": "No waypoints set."})
    plan = router.simulate(app.ship.current_ship(), app.ship.current_skills(),
                           wps, app.route.modes(), app.route.strategy())
    return json.dumps({
        "waypoints": [s.name for s in wps],
        "ship": app.ship.current_ship().name,
        "total_fuel": plan.total_fuel,
        "total_minutes": round(plan.total_time_min),
        "jumps": plan.jumps,
        "gates": plan.gates,
        "all_legs_possible": plan.all_in_range,
        "peak_fatigue_min": round(plan.peak_fatigue_min),
        "legs": [{"from": lg.src.name, "to": lg.dst.name, "mode": lg.mode,
                  "ly": round(lg.distance_ly, 2), "fuel": lg.fuel,
                  "possible": lg.in_range, "problem": lg.reason or None}
                 for lg in plan.legs],
    })


def list_ships(app) -> str:
    from ..data.ships import SHIPS
    return json.dumps([{"name": s.name, "class": s.hull_class, "race": s.race,
                        "base_range_ly": s.base_range_ly,
                        "fuel_per_ly": s.base_fuel_per_ly,
                        "isotope": s.isotope,
                        "is_freighter": s.is_freighter,
                        "can_bridge": s.bridge_range_ly is not None}
                       for s in SHIPS])


def get_setup(app) -> str:
    st = app.ship.state()
    chars = app.waypoint_menu_targets()
    loc = app.universe.systems.get(app.location_system_id) if app.universe else None
    return json.dumps({
        "ship": st.get("ship"), "skills": {k: st[k] for k in
                                           ("jdc", "jdo", "jfc", "jf")},
        "prefer": app.route.strategy(),
        "allow_gates": app.route.chk_gates.isChecked(),
        "use_ansiblex": app.route.use_ansiblex(),
        "gate_preference": app.route.gate_pref(),
        "avoid_incursions": app.route.avoid_incursions(),
        "steer_around_kills": app.route.avoid_kills(),
        "pick_docks": app.route.pick_docks(),
        "linked_characters": [n for _, n in chars] if chars else [],
        "current_location": loc.name if loc else None,
        "avoided_systems": config.get_avoided(),
    })


def systems_in_jump_range(app, name: str) -> str:
    s = _resolve(app, name)
    rng = app.ship.reach_range()
    out = [{"name": t.name, "ly": round(d, 2), "security": round(t.security, 2)}
           for t, d in app.universe.within_range(s, rng)]
    out.sort(key=lambda r: r["ly"])
    return json.dumps({"from": s.name, "range_ly": round(rng, 2),
                       "ship": app.ship.current_ship().name,
                       "count": len(out), "systems": out[:120]})


def list_saved_routes(app) -> str:
    return json.dumps([{"name": n, "stops": d.get("systems", []),
                        "ship": d.get("ship")}
                       for n, d in config.get_saved_routes().items()])


# -- write tools ------------------------------------------------------------
def add_waypoint(app, name: str) -> str:
    s = _resolve(app, name)
    app.route.add_system(s.id)
    return f"Added {s.name}. Route is now: " + \
           ", ".join(w.name for w in app.route.systems())


def remove_waypoint(app, name: str) -> str:
    s = _resolve(app, name)
    app.route.remove_system(s.id)
    return f"Removed {s.name}. Route is now: " + \
           (", ".join(w.name for w in app.route.systems()) or "(empty)")


def clear_waypoints(app) -> str:
    app.route._clear()
    return "Cleared all waypoints."


def set_ship(app, name: str) -> str:
    from ..data.ships import SHIPS_BY_NAME
    match = next((n for n in SHIPS_BY_NAME if n.lower() == name.lower()), None)
    if match is None:
        raise ValueError(f"Unknown ship {name!r}. Call list_ships first.")
    app.ship.restore(dict(app.ship.state(), ship=match))
    return f"Ship set to {match}."


def set_options(app, prefer: str = "", allow_gates: bool | None = None,
                use_ansiblex: bool | None = None, gate_preference: str = "",
                avoid_incursions: bool | None = None,
                steer_around_kills: bool | None = None,
                pick_docks: bool | None = None) -> str:
    r = app.route
    changed = []
    if prefer:
        i = r.cmb_balance.findText(prefer, __import__("PySide6").QtCore.Qt.
                                   MatchFlag.MatchContains)
        if i < 0:
            raise ValueError("prefer must be one of: " + ", ".join(
                r.cmb_balance.itemText(k) for k in range(r.cmb_balance.count())))
        r.cmb_balance.setCurrentIndex(i)
        changed.append(f"balance={r.cmb_balance.currentText()}")
    for value, widget, label in (
            (allow_gates, r.chk_gates, "allow_gates"),
            (use_ansiblex, r.chk_ansiblex, "use_ansiblex"),
            (avoid_incursions, r.chk_incursions, "avoid_incursions"),
            (steer_around_kills, r.chk_kills, "steer_around_kills"),
    ):
        if value is not None:
            widget.setChecked(bool(value))
            changed.append(f"{label}={bool(value)}")
    if pick_docks is not None:
        r.chk_nodocks.setChecked(not pick_docks)
        changed.append(f"pick_docks={bool(pick_docks)}")
    if gate_preference:
        i = r.cmb_gate.findText(gate_preference,
                                __import__("PySide6").QtCore.Qt.MatchFlag.MatchContains)
        if i < 0:
            raise ValueError("gate_preference must be one of: " + ", ".join(
                r.cmb_gate.itemText(k) for k in range(r.cmb_gate.count())))
        r.cmb_gate.setCurrentIndex(i)
        changed.append(f"gate_preference={r.cmb_gate.currentText()}")
    return "Updated: " + (", ".join(changed) or "nothing")


def auto_route(app) -> str:
    """Bridge between the existing waypoints, then report the plan.

    Calls _auto_route(), not auto_route() -- MainWindow has no public
    method by that name, only the underscored one, and nothing had ever
    called it through the bridge until now to catch that. _auto_route is
    also the entry point on purpose rather than the private _do_auto_route
    it eventually delegates to: it runs the high-sec-destination guard and
    the docking-aware station-data load first, and skipping straight to
    _do_auto_route would silently skip both.

    The high-sec guard is duplicated here, ahead of the call, because
    _auto_route's own version of it is a real, blocking QMessageBox.warning()
    -- exactly right for a click from the UI, but reached through the
    bridge's synchronous marshal it would pop a modal on the user's screen
    and hang this tool call for up to the bridge's own timeout waiting for a
    click nobody watching this conversation can give it. Answering here
    instead, before ever reaching that call, is what keeps this a normal
    tool response rather than a stuck dialog.
    """
    if len(app.route.waypoints) >= 2:
        from ..data import docking
        dest = app.route.waypoints[-1].system
        ship = app.ship.current_ship()
        if dest.security >= 0.5 and not docking.can_use_highsec_gates(ship):
            return (f"Can't auto-route: {dest.name} is high-sec. Capitals "
                    "cannot enter high-sec (no high-sec gates, and jump "
                    "drives can't activate into high-sec). Only jump "
                    "freighters can gate the final high-sec leg -- pick a "
                    "low/null staging system instead, or switch to a jump "
                    "freighter with set_ship.")

    # _auto_route() starts a background Worker and returns immediately, which
    # is right for a click -- the UI has its own spinner and stays responsive.
    # Called through the bridge's own nested QEventLoop marshal, "started,
    # check back later" turned out not to be reliable: the Worker's
    # finished_ok arrives strictly after that nested loop has already quit
    # and handed control back to the bridge thread, and nothing then pumps
    # the main thread's queue to actually deliver it -- confirmed by direct
    # comparison, the exact same call landed correctly when made directly and
    # never landed at all through the bridge with a several-second wait after.
    # route.busy is already the real, existing signal for "still computing"
    # (shown right before the Worker starts, hidden in both its finished and
    # failed callbacks), so poll that instead of guessing at a fixed delay --
    # pumping events here is also what actually lets the queued signal be
    # delivered at all, which a bare time.sleep would not do.
    from PySide6.QtCore import QCoreApplication
    import time as _time

    app._auto_route()
    deadline = _time.time() + 20.0
    QCoreApplication.processEvents()
    while app.route.busy.isVisible() and _time.time() < deadline:
        QCoreApplication.processEvents()
        _time.sleep(0.05)
    if app.route.busy.isVisible():
        return ("Auto-route is still computing after 20s. Call get_route "
                "shortly to check on it.")
    return get_route(app)


def set_system_note(app, name: str, note: str) -> str:
    s = _resolve(app, name)
    config.set_system_note(s.name, note)
    app.refresh_notes()
    return f"Note for {s.name} " + ("cleared." if not note.strip() else "saved.")


def avoid_system(app, name: str, avoid: bool = True) -> str:
    s = _resolve(app, name)
    if app.is_avoided(s.id) != bool(avoid):
        app.toggle_avoid(s.id)
    return f"{s.name} is now {'avoided' if avoid else 'not avoided'}."


# ---------------------------------------------------------------------------
TOOLS: list[Tool] = [
    Tool("find_system",
         "Look up one solar system: security, region, sovereignty, whether a "
         "capital can jump into it, and any note the user wrote. Call this "
         "whenever the user names a system you have not already looked up.",
         _obj({"name": _SYSTEM}, ["name"]), find_system),

    Tool("system_intel",
         "Activity signals for a system: gate traffic, NPC kills (ratting), "
         "player and pod kills, sovereignty ADM and the industry index. Call "
         "this for any question about whether a system is busy, dangerous, "
         "ratted, mined or lived in. The reply carries the caveats; repeat "
         "them rather than presenting these as player counts.",
         _obj({"name": _SYSTEM}, ["name"]), system_intel),

    Tool("cyno_activity",
         "Check killmails for ships that died with a cyno fitted in one "
         "system. Costs a slow external request, so call it only when the "
         "user asks about cynos or hotdrop risk specifically.",
         _obj({"name": _SYSTEM}, ["name"]), cyno_activity),

    Tool("get_route",
         "The current waypoints and the computed plan: fuel, time, jumps, "
         "gates, per-leg detail and why any leg is impossible. Call this "
         "before answering questions about the route, and again after "
         "changing it to confirm the result.",
         _obj({}), get_route),

    Tool("get_setup",
         "Current ship, jump skills, routing preferences, linked characters, "
         "in-game location and the avoid list. Call this before changing any "
         "setting so you know what it already is.",
         _obj({}), get_setup),

    Tool("list_ships",
         "Every ship the planner knows, with its class and base jump range. "
         "Call this before set_ship so you use a name that exists.",
         _obj({}), list_ships),

    Tool("systems_in_jump_range",
         "Systems reachable in one jump from a given system with the current "
         "ship and skills. Use it to find a staging point or a midpoint when "
         "a leg is out of range.",
         _obj({"name": _SYSTEM}, ["name"]), systems_in_jump_range),

    Tool("list_saved_routes",
         "The user's saved routes, each with its stops and the ship it was "
         "saved for. Call this when they refer to a route by name.",
         _obj({}), list_saved_routes),

    Tool("add_waypoint",
         "Append a system to the route. Waypoints are kept in the order "
         "added; the first is the origin.",
         _obj({"name": _SYSTEM}, ["name"]), add_waypoint, writes=True),

    Tool("remove_waypoint",
         "Drop one system from the route, leaving the rest in order. Use this "
         "rather than clearing and rebuilding when only one stop is wrong.",
         _obj({"name": _SYSTEM}, ["name"]), remove_waypoint, writes=True),

    Tool("clear_waypoints",
         "Remove every waypoint and start the route over. Destructive: prefer "
         "remove_waypoint unless the user asked to start again.",
         _obj({}), clear_waypoints, writes=True),

    Tool("set_ship",
         "Change the ship the route is planned for. Jump range, fuel and "
         "which gates are usable all follow from this, so set it before "
         "reading a plan the user asked about for a specific hull.",
         _obj({"name": {"type": "string",
                        "description": "Ship name from list_ships."}}, ["name"]),
         set_ship, writes=True),

    Tool("set_options",
         "Change routing preferences. Omit anything you do not want to "
         "change. Call get_setup first to see the current values and the "
         "accepted strings for prefer and gate_preference.",
         _obj({
             "prefer": {"type": "string",
                        "description": "Jump/gate balance preset, e.g. 'Prefer jumps'."},
             "allow_gates": {"type": "boolean"},
             "use_ansiblex": {"type": "boolean"},
             "gate_preference": {"type": "string",
                                 "description": "e.g. 'Fastest' or 'Safer'."},
             "avoid_incursions": {"type": "boolean"},
             "steer_around_kills": {"type": "boolean"},
             "pick_docks": {"type": "boolean",
                            "description": "False means just passing through."},
         }), set_options, writes=True),

    Tool("auto_route",
         "Fill in the systems needed between the existing waypoints. Use it "
         "after setting an origin and a destination, or when a leg is out of "
         "jump range.",
         _obj({}), auto_route, writes=True),

    Tool("set_system_note",
         "Save the user's own note against a system (gate camp, friendly "
         "structure, cyno alt parked here). Pass an empty note to delete it.",
         _obj({"name": _SYSTEM, "note": {"type": "string"}}, ["name", "note"]),
         set_system_note, writes=True),

    Tool("avoid_system",
         "Add or remove a system from the never-route-through list.",
         _obj({"name": _SYSTEM, "avoid": {"type": "boolean"}}, ["name"]),
         avoid_system, writes=True),
]

BY_NAME = {t.name: t for t in TOOLS}


SYSTEM_PROMPT = """You are the routing assistant inside Eve-Strait, a desktop \
capital and jump-freighter route planner for EVE Online. You are talking to \
the pilot, in their own app, about their own route.

Use the tools rather than answering from memory. The app holds live ESI data \
and the user's actual configuration; your recollection of EVE does not.

Mechanics that are easy to get wrong, and that the tools will confirm:
- A capital can jump OUT of high-sec but never INTO it. Only jump freighters \
may use high-sec gates at all.
- A cyno jammer makes a system unreachable by jump entirely.
- ESI publishes no per-system player count and nothing at all about cynos. \
Gate traffic and ADM are proxies; cyno figures come from killmails and are a \
floor, never a count. Say so rather than implying precision the data lacks.

Before changing anything, read the current state with get_setup or get_route. \
After changing the route, call get_route and tell the user what it actually \
costs in fuel and time. Keep replies short: the user is looking at the map."""
