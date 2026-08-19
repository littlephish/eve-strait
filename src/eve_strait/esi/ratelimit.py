"""Rate-limit decision logic for ESI. Pure functions and state, no I/O.

Deliberately free of sleeping and networking: the transport acts on the
decisions this module returns. That keeps the maths testable without a clock.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

# CCP expresses limits as "<tokens>/<window>", e.g. "150/15m".
_LIMIT_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*([smh])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}

# Path segments that are pure digits are IDs; collapse them so that
# /universe/stations/60003760/ and /universe/stations/60003761/ share state.
_ID_SEGMENT_RE = re.compile(r"/\d+")

# Cost of a successful response, per CCP: 2XX = 2 tokens. Used to convert a
# remaining-token count into a remaining-request count.
TOKENS_PER_REQUEST = 2

PACE_BELOW = 0.50        # start spreading requests below half the budget
RESERVE_FLOOR = 0.10     # below this, only interactive work may spend
MAX_PACING_DELAY = 60.0  # never stall a single request longer than this
POLL_FLOOR_SECONDS = 30.0
BACKGROUND_SHARE = 0.50  # fraction of a group's budget timers may use
MAX_INTERACTIVE_WAIT = 60.0   # longer than this, tell the user instead of stalling
ERROR_LIMIT_FLOOR = 10        # X-ESI-Error-Limit-Remain below this parks everything


@dataclass(frozen=True)
class Limit:
    max_tokens: int
    window_seconds: int


@dataclass(frozen=True)
class Decision:
    action: str            # "proceed" | "wait" | "decline"
    seconds: float = 0.0
    reason: str = ""


@dataclass
class _GroupState:
    limit: Limit | None = None
    remaining: int | None = None
    parked_until: float = 0.0


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


class RateLimitGovernor:
    """Tracks per-bucket budget and says whether a request may go now.

    Buckets mirror CCP's own keying: authenticated routes bucket per
    (group, application, character), so character_id is part of the key.
    """

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._groups: dict[tuple[str, object], _GroupState] = {}
        # Learned from responses: which group a route belongs to. Until a
        # route has been seen once we cannot know, so it stays optimistic.
        self._route_groups: dict[str, str] = {}
        self._error_parked_until = 0.0

    # -- keying -------------------------------------------------------------
    def _key(self, path: str, character_id: int | None):
        rk = route_key(path)
        group = self._route_groups.get(rk, rk)
        return (group, character_id if character_id is not None else "anon")

    # -- observation --------------------------------------------------------
    def observe(self, path: str, character_id: int | None, headers) -> None:
        """Fold one response's rate-limit headers into the governor."""
        group = headers.get("X-Ratelimit-Group")
        rk = route_key(path)
        with self._lock:
            if group:
                self._route_groups[rk] = group
            key = (group or rk, character_id if character_id is not None else "anon")
            st = self._groups.setdefault(key, _GroupState())
            limit = parse_limit(headers.get("X-Ratelimit-Limit"))
            if limit:
                st.limit = limit
            remaining = headers.get("X-Ratelimit-Remaining")
            if remaining is not None:
                try:
                    st.remaining = int(remaining)
                except (TypeError, ValueError):
                    pass

    # -- decisions ----------------------------------------------------------
    def park(self, path: str, character_id: int | None, seconds: float) -> None:
        """Stop using a bucket until Retry-After has elapsed."""
        with self._lock:
            key = self._key(path, character_id)
            st = self._groups.setdefault(key, _GroupState())
            st.parked_until = max(st.parked_until, self._clock() + seconds)

    def observe_errors(self, headers) -> None:
        """The error limit is a separate, global, fixed-window budget.

        Exhausting it makes ESI discard every request until the window ends,
        and because 4XX also costs 5 tokens an error storm drains both
        budgets at once. So we park everything, not just one group.
        """
        try:
            remain = int(headers.get("X-ESI-Error-Limit-Remain", ""))
            reset = float(headers.get("X-ESI-Error-Limit-Reset", ""))
        except (TypeError, ValueError):
            return
        if remain < ERROR_LIMIT_FLOOR:
            with self._lock:
                self._error_parked_until = max(self._error_parked_until,
                                               self._clock() + reset)

    def check(self, path: str, character_id: int | None,
              priority: str = "interactive") -> Decision:
        with self._lock:
            now = self._clock()
            st = self._groups.get(self._key(path, character_id))
            parked_until = max(self._error_parked_until,
                               st.parked_until if st else 0.0)
            if parked_until > now:
                if priority == "background":
                    return Decision("decline", reason="parked")
                return Decision("wait", parked_until - now, "parked")
            if st is None or st.limit is None or st.remaining is None:
                return Decision("proceed")          # optimistic until observed
            fraction = st.remaining / st.limit.max_tokens
            if fraction >= PACE_BELOW:
                return Decision("proceed")
            if fraction < RESERVE_FLOOR and priority == "background":
                return Decision("decline", reason="reserve floor")
            requests_left = max(st.remaining / TOKENS_PER_REQUEST, 1.0)
            delay = min(st.limit.window_seconds / requests_left, MAX_PACING_DELAY)
            return Decision("wait", delay, "pacing")

    def poll_interval(self, path: str, character_id: int | None,
                      floor: float = POLL_FLOOR_SECONDS) -> float:
        """How often a background timer may hit this route."""
        with self._lock:
            st = self._groups.get(self._key(path, character_id))
            if st is None or st.limit is None:
                return floor
            budget = st.limit.max_tokens * BACKGROUND_SHARE
            requests = max(budget / TOKENS_PER_REQUEST, 1.0)
            return max(st.limit.window_seconds / requests, floor)
