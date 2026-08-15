"""Dotlan jump-planner links, both directions.

Dotlan is the lingua franca for sharing a capital route, so a link is worth
more than any format of ours: paste it in a corp channel and everyone can open
it, whether or not they run this app.

    https://evemaps.dotlan.net/jump/Revelation_Navy_Issue,544/UW9B-F:Y-DW5K:Utopia

    /jump/ <ship name, spaces as underscores> , <JDC><JFC><JF> / <names, ':'>

The three digits are Jump Drive Calibration, Jump Fuel Conservation and Jump
Freighter, in that order -- the three skills Dotlan's own Jump Options offer.
Jump Drive Operation is not among them because it moves capacitor rather than
range or fuel, so importing a link leaves ours alone rather than inventing a
value for it.

Verified against the live page rather than assumed: Revelation Navy Issue at
JDC 5 shows a 7 ly maximum, which is a 3.5 ly hull at +20% per level, and the
seven waypoints in the example come back in order.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, quote

BASE = "https://evemaps.dotlan.net/jump"

# /jump/Ship,544/A:B:C  -- the skills group is optional, and so is the route.
_PATH = re.compile(
    r"/jump/(?P<ship>[^/,]+)(?:,(?P<skills>\d{1,3}))?(?:/(?P<route>[^/?#]*))?",
    re.IGNORECASE)


class DotlanRoute:
    """What a link carries. Any field may be None if the link omitted it."""

    def __init__(self, ship: str | None, jdc: int | None, jfc: int | None,
                 jf: int | None, systems: list[str]):
        self.ship = ship
        self.jdc = jdc
        self.jfc = jfc
        self.jf = jf
        self.systems = systems

    def __repr__(self) -> str:
        return (f"DotlanRoute(ship={self.ship!r}, jdc={self.jdc}, "
                f"jfc={self.jfc}, jf={self.jf}, systems={self.systems!r})")


def build_url(ship: str | None, jdc: int | None, jfc: int | None,
              jf: int | None, systems) -> str:
    """A shareable Dotlan link for this ship, these skills and this route."""
    names = [str(s) for s in systems if str(s).strip()]
    part = quote(str(ship or "").replace(" ", "_"), safe="-_'")
    if jdc is not None and jfc is not None and jf is not None:
        part += f",{_digit(jdc)}{_digit(jfc)}{_digit(jf)}"
    url = f"{BASE}/{part}" if part else BASE
    if names:
        url += "/" + ":".join(quote(n, safe="-_'") for n in names)
    return url


def _digit(value) -> int:
    try:
        return max(0, min(5, int(value)))
    except (TypeError, ValueError):
        return 0


def parse_url(text: str) -> DotlanRoute | None:
    """Read a Dotlan jump link. Returns None if it is not one.

    Tolerant on purpose: people paste links with a scheme or without, with a
    trailing slash, wrapped in angle brackets by a chat client, or with the
    whole thing URL-encoded.
    """
    if not text:
        return None
    raw = text.strip().strip("<>").strip()
    if "/jump/" not in raw:
        return None
    m = _PATH.search(raw)
    if not m:
        return None

    ship = unquote(m.group("ship") or "").replace("_", " ").strip() or None
    jdc = jfc = jf = None
    skills = m.group("skills")
    if skills:
        # Short groups are read from the left, which is the order Dotlan
        # writes them: calibration first.
        padded = skills.ljust(3, "0")
        jdc, jfc, jf = (int(c) for c in padded[:3])

    systems: list[str] = []
    route = m.group("route") or ""
    for chunk in route.split(":"):
        name = unquote(chunk).replace("_", " ").strip()
        if name:
            systems.append(name)
    return DotlanRoute(ship, jdc, jfc, jf, systems)
