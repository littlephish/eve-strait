"""Tripwire integration: route over your group's own scanned chain.

Tripwire (tripwiremap.app, or a copy your alliance hosts) is the most widely
used wormhole mapper in EVE. Where EVE-Scout publishes the two public hubs and
Wanderer covers self-hosted maps, Tripwire is what most groups actually run.

It is also the best-informed of the three for routing, because it reports the
things the other two leave out:

* **signatures at both ends** of every connection, which Wanderer does not
  carry at all -- without them a wormhole leg cannot be flown, only admired;
* **life and mass status**, which EVE-Scout does not report;
* a modification timestamp, so a leg can say how long ago anyone looked.

**Authentication is a username and password, not a token.** Tripwire has its
own accounts and its API rides a PHP session:

    POST {url}/login.php     {username, password, mode: login}  -> session
    GET  {url}/refresh.php?systemID=...                         -> JSON

That is materially different from ESI, which hands out scoped, revocable
tokens. A Tripwire password is neither, so it is never written to this app's
config: see ``config.tripwire_password``, which keeps it in the operating
system's credential store.

**J-space chains are collapsed**, the same way ``wanderer.edges`` does it and
for the same reason: this app only loads k-space, so a chain running
k -> J -> J -> k would otherwise be discarded entirely -- and those are the
interesting ones. Each k-space end is walked outward through J-space to
whatever k-space it reaches, and the whole path becomes one edge costing the
hops it really takes.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections import deque

from .. import config
from . import evescout

_UA = "eve-strait/0.1 (+https://github.com/littlephish/eve-strait)"
_STORE = config.CACHE_DIR / "tripwire.json"

# A chain gets long and every extra hop is another chance the far end has
# already collapsed. Past this it is not a shortcut any more. Matches
# wanderer.MAX_HOPS deliberately: the same judgement about the same thing.
MAX_HOPS = 6

# Tripwire's own vocabulary, mapped onto the keys evescout.usable() filters
# on, so a hole is judged the same however it was found.
LIFE_STATUS = {"stable": "fresh", "critical": "end of life"}
MASS_STATUS = {"stable": "fresh", "destab": "reduced", "critical": "critical"}

# What a hole of unknown type is assumed to pass. Same reasoning as
# evescout._LABEL_MIN_T: guessing generously is how a freighter gets routed
# into a hole it bounces off.
UNKNOWN_MAX_T = 62_000


def login(url: str, username: str, password: str, timeout: float = 20.0):
    """Open a Tripwire session. Returns an opener carrying the cookie.

    Raises on failure rather than returning an empty session: "could not log
    in" and "logged in, chain is empty" are very different answers to "do I
    have a way home", and must not look alike.
    """
    import http.cookiejar

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))
    data = urllib.parse.urlencode({
        "username": username,
        "password": password,
        "mode": "login",
    }).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/login.php", data=data,
        headers={"User-Agent": _UA,
                 "Content-Type": "application/x-www-form-urlencoded"})
    with opener.open(req, timeout=timeout) as resp:
        resp.read()
    if not len(jar):
        raise RuntimeError(
            "Tripwire did not return a session cookie - check the username "
            "and password, and that the URL is the site root.")
    return opener


def fetch(url: str, opener, system_id: int = 30000142,
          timeout: float = 30.0) -> dict:
    """Pull the chain as Tripwire sees it. Raises on failure.

    ``system_id`` is the system Tripwire centres the chain on; it defaults to
    Jita only because the endpoint requires one. The response carries the
    whole map the account can see, not merely that system's neighbours.
    """
    query = urllib.parse.urlencode({"mode": "init", "systemID": str(system_id)})
    req = urllib.request.Request(
        f"{url.rstrip('/')}/refresh.php?{query}",
        headers={"User-Agent": _UA, "Accept": "application/json"})
    with opener.open(req, timeout=timeout) as resp:
        data = json.load(resp)
    data["fetched"] = time.time()
    return data


def _max_t(wh_type) -> int:
    """Heaviest single ship this connection will pass.

    The type code decides where there is one, reusing evescout's table for
    exactly the reason documented there. K162 is the generic exit signature
    and says nothing about size -- the far side carries the real code -- so it
    falls through to the conservative default.
    """
    code = (wh_type or "").strip().upper()
    if code and code != "K162":
        known = evescout.WH_MAX_JUMP_T.get(code)
        if known:
            return known
    return UNKNOWN_MAX_T


def _links(data: dict) -> list[dict]:
    """Flatten Tripwire's two tables into one list of connections.

    ``wormholes`` references ``signatures`` by id at each end, so neither
    table alone says what connects to what.
    """
    sigs = (data or {}).get("signatures") or {}
    out = []
    for wh in ((data or {}).get("wormholes") or {}).values():
        a = sigs.get(str(wh.get("initialID")))
        b = sigs.get(str(wh.get("secondaryID")))
        if not a or not b:
            continue        # dangling half of a connection; nothing to fly
        try:
            a_sys, b_sys = int(a.get("systemID")), int(b.get("systemID"))
        except (TypeError, ValueError):
            continue
        if a_sys == b_sys:
            continue
        out.append({
            "a_sys": a_sys, "b_sys": b_sys,
            "a_sig": _clean_sig(a.get("signatureID")),
            "b_sig": _clean_sig(b.get("signatureID")),
            "max_t": _max_t(wh.get("type")),
            "life": LIFE_STATUS.get((wh.get("life") or "").lower()),
            "mass": MASS_STATUS.get((wh.get("mass") or "").lower()),
            "updated_at": _newest(a.get("modifiedTime"),
                                  b.get("modifiedTime")),
        })
    return out


def _clean_sig(value):
    """A usable signature, or None.

    Tripwire stores "???" for a signature nobody has filled in yet. That is
    an absence, and must read as one rather than as an id to look for.
    """
    text = (value or "").strip()
    if not text or set(text) <= {"?"}:
        return None
    return text.upper()


def _newest(*stamps):
    """The most recent of several Tripwire timestamps, or None."""
    best, best_age = None, None
    for stamp in stamps:
        age = evescout.age_hours(stamp)
        if age is None:
            continue
        if best_age is None or age < best_age:
            best, best_age = stamp, age
    return best


def edges(data: dict, systems) -> dict:
    """Collapse the chain into k-space edges the router can use.

    ``systems`` is Universe.systems; membership in it is the k-space test,
    since J-space is never loaded.

    Returns {(a, b): info} in the same shape evescout.graph produces, so the
    three sources merge and the router does not care where an edge came from.
    """
    adjacency: dict[int, list[dict]] = {}
    for link in _links(data):
        adjacency.setdefault(link["a_sys"], []).append(
            {"to": link["b_sys"], "link": link, "from": link["a_sys"]})
        adjacency.setdefault(link["b_sys"], []).append(
            {"to": link["a_sys"], "link": link, "from": link["b_sys"]})

    out: dict[tuple[int, int], dict] = {}

    def add(a: int, b: int, info: dict):
        key = (a, b) if a < b else (b, a)
        old = out.get(key)
        # Prefer the roomier way through, then the shorter one: a chain that
        # takes a freighter beats a shorter one that does not.
        if (old is None or info["max_t"] > old["max_t"]
                or (info["max_t"] == old["max_t"]
                    and info["hops"] < old["hops"])):
            out[key] = info

    for start in list(adjacency):
        if start not in systems:
            continue                    # only walk outward from k-space
        # BFS through J-space only. Reaching k-space ends that branch: the
        # router can plan onward from there itself.
        queue = deque([(start, 0, None, None, None, None, None)])
        seen = {start}
        while queue:
            node, hops, worst, life, mass, stamp, first_sig = queue.popleft()
            if hops >= MAX_HOPS:
                continue
            for hop in adjacency.get(node, ()):
                nxt, link = hop["to"], hop["link"]
                if nxt in seen:
                    continue
                limit = (link["max_t"] if worst is None
                         else min(worst, link["max_t"]))
                # Worst status along the chain: a route is only as safe as its
                # shakiest hole, and a warning anywhere applies to the whole.
                w_life = evescout._worst(life, link["life"], evescout._LIFE_RANK)
                w_mass = evescout._worst(mass, link["mass"], evescout._MASS_RANK)
                oldest = _oldest(stamp, link["updated_at"])
                # The signature to warp to where you are standing now.
                near = (first_sig if first_sig is not None
                        else (link["a_sig"] if hop["from"] == link["a_sys"]
                              else link["b_sig"]))
                if nxt in systems:
                    if nxt != start:
                        far = (link["b_sig"] if hop["from"] == link["a_sys"]
                               else link["a_sig"])
                        add(start, nxt, {
                            "via": "Tripwire", "hops": hops + 1,
                            "size": "unknown", "wh_types": [],
                            "max_t": limit,
                            "sigs": {k: v for k, v in
                                     ((start, near), (nxt, far)) if v},
                            "hours": None,
                            "life": w_life, "mass": w_mass,
                            "updated_at": oldest,
                            "updated_at_all": [oldest] if oldest else [],
                        })
                    continue            # do not route *through* other k-space
                seen.add(nxt)
                queue.append((nxt, hops + 1, limit, w_life, w_mass,
                              oldest, near))
    return out


def _oldest(*stamps):
    """The stalest of several timestamps -- a chain is as old as its worst."""
    best, best_age = None, None
    for stamp in stamps:
        age = evescout.age_hours(stamp)
        if age is None:
            continue
        if best_age is None or age > best_age:
            best, best_age = stamp, age
    return best


def refresh(timeout: float = 30.0) -> dict:
    """Log in and fetch using the stored settings. {} when not configured.

    The password comes from the OS credential store rather than the config,
    so "configured" can be true while the password is missing -- the user
    declined to have it remembered. That is a prompt, not an error, and the
    caller distinguishes them by the exception message.
    """
    url = config.get_tripwire_url()
    user = config.get_tripwire_user()
    password = config.get_tripwire_password()
    if not (url and user):
        return {}
    if not password:
        raise RuntimeError("No saved Tripwire password - open Settings to "
                           "enter it.")
    opener = login(url, user, password, timeout=timeout)
    data = fetch(url, opener, timeout=timeout)
    save(data)
    return data


def configured() -> bool:
    """Whether there is enough stored to attempt a fetch at all."""
    return bool(config.get_tripwire_url() and config.get_tripwire_user())


def describe(data: dict) -> str:
    """One line for the status bar."""
    sigs = len((data or {}).get("signatures") or {})
    whs = len((data or {}).get("wormholes") or {})
    return f"Tripwire: {whs} connection(s) across {sigs} signature(s)."


# -- cache ------------------------------------------------------------------
def load() -> dict:
    try:
        return json.loads(_STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    try:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        _STORE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
