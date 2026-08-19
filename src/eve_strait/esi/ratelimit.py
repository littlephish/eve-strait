"""Rate-limit decision logic for ESI. Pure functions and state, no I/O.

Deliberately free of sleeping and networking: the transport acts on the
decisions this module returns. That keeps the maths testable without a clock.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# CCP expresses limits as "<tokens>/<window>", e.g. "150/15m".
_LIMIT_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*([smh])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}

# Path segments that are pure digits are IDs; collapse them so that
# /universe/stations/60003760/ and /universe/stations/60003761/ share state.
_ID_SEGMENT_RE = re.compile(r"/\d+")


@dataclass(frozen=True)
class Limit:
    max_tokens: int
    window_seconds: int


def parse_limit(raw: str | None) -> Limit | None:
    """Parse an X-Ratelimit-Limit value. None on anything unexpected.

    Junk is not an error worth raising: an unparseable limit just means we
    stay optimistic, which is the same state we start in.
    """
    if not raw:
        return None
    m = _LIMIT_RE.match(raw)
    if not m:
        return None
    tokens, size, unit = m.groups()
    return Limit(int(tokens), int(size) * _UNIT_SECONDS[unit])


def route_key(path: str) -> str:
    """A stable key for a route, with concrete IDs replaced by {id}."""
    return _ID_SEGMENT_RE.sub("/{id}", path)
