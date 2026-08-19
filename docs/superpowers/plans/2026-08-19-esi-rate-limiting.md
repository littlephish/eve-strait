# ESI Rate Limiting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every ESI call rate-limit aware and cache-honouring, so the cyno alt scan and dockables refresh stop returning HTTP 429.

**Architecture:** A new transport layer under `src/eve_strait/esi/` splits into three focused modules: `ratelimit.py` (pure decision logic, no I/O), `httpcache.py` (sqlite storage, no HTTP), and `transport.py` (the façade that combines them with `requests`). `client.py` stops calling `requests` directly and delegates to the transport. Cache lifetime comes from ESI's own `expires` header with a small override table.

**Tech Stack:** Python 3.11-3.13, `requests`, stdlib `sqlite3`, `pytest` (new dev dependency), PySide6 for the two UI touch points.

**Spec:** [docs/superpowers/specs/2026-08-19-esi-rate-limiting-design.md](../specs/2026-08-19-esi-rate-limiting-design.md)

## Global Constraints

- Python `>=3.11,<3.14`. No new **runtime** dependencies — sqlite3 is stdlib. `pytest` is dev-only.
- Do not touch `data/universe.py` (Fuzzwork), `esi/images.py` (images.evetech.net), `esi/evescout.py`, `esi/wanderer.py`, `esi/zkill.py`. None are ESI routes.
- The app must keep working without an ESI login. Login is an enhancement, never a prerequisite.
- Windows-first. Paths come from `config.CACHE_DIR`, never hardcoded.
- `ratelimit.py` must contain no `time.sleep` and no network calls — it returns decisions, the transport acts on them. This is what makes it testable without a clock.
- Token costs: 2XX = 2, 3XX = 1, 4XX = 5, 5XX = 0. Window is floating, typically 15 minutes.
- Reserve floor is **10%**; pacing starts below **50%** remaining; per-request delay caps at **60s**; interactive waits on a park only when `Retry-After` <= **60s**.
- Polling interval floor is **30s**.
- Commits must not mention AI assistance or add `Co-Authored-By` trailers.

---

### Task 1: Rate-limit header parsing (plus test scaffolding)

Sets up the test suite alongside the first piece of real logic, because a test harness with nothing to test is not an independently reviewable deliverable.

**Files:**
- Create: `src/eve_strait/esi/ratelimit.py`
- Create: `tests/test_ratelimit.py`
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: nothing.
- Produces: `Limit(max_tokens: int, window_seconds: int)` frozen dataclass; `parse_limit(raw: str) -> Limit | None`; `route_key(path: str) -> str`.

- [ ] **Step 1: Add the dev dependency and CI step**

In `pyproject.toml`, after the `[project.scripts]` block, add:

```toml
[dependency-groups]
dev = [
    "pytest>=8",
]
```

In `.github/workflows/ci.yml`, add this step after the "Import sanity" step:

```yaml
      - name: Unit tests
        run: |
          python -m pip install pytest
          python -m pytest -q
```

In `.github/workflows/ci.yml`, the header comment currently says Eve-Strait "has no test suite committed to the repo yet". Replace that paragraph with:

```yaml
# This is a fast sanity gate: it byte-compiles the package, confirms every
# module imports, and runs the unit tests. The suite covers the ESI transport
# layer only (rate-limit maths, cache expiry) -- logic where a silent
# off-by-one is invisible when running the app. Everything else is still
# verified by hand.
```

In `AGENTS.md`, under "Running", replace the line beginning "There is **no committed test suite**" with:

```markdown
- The committed test suite (`tests/`, run with `uv run pytest`) covers the ESI
  transport layer only: rate-limit maths and cache expiry. It is deliberately
  narrow — pure logic, no network, no Qt. Everything else is still verified by
  running the app.
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_ratelimit.py`:

```python
import pytest

from eve_strait.esi.ratelimit import Limit, parse_limit, route_key


@pytest.mark.parametrize("raw,expected", [
    ("150/15m", Limit(150, 900)),
    ("1000/15m", Limit(1000, 900)),
    ("1000/1h", Limit(1000, 3600)),
    ("60/30s", Limit(60, 30)),
])
def test_parse_limit_understands_ccp_format(raw, expected):
    assert parse_limit(raw) == expected


@pytest.mark.parametrize("raw", ["", "nonsense", "150", "150/15x", "abc/15m", None])
def test_parse_limit_returns_none_on_junk(raw):
    assert parse_limit(raw) is None


def test_route_key_collapses_numeric_ids():
    assert route_key("/characters/12345/assets/") == "/characters/{id}/assets/"
    assert route_key("/universe/stations/60003760/") == "/universe/stations/{id}/"


def test_route_key_leaves_static_paths_alone():
    assert route_key("/sovereignty/map/") == "/sovereignty/map/"
```

Why `route_key` matters: without collapsing IDs, every station lookup would be its own governor key and we would never accumulate enough observations to pace anything.

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_ratelimit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'eve_strait.esi.ratelimit'`

- [ ] **Step 4: Write minimal implementation**

Create `src/eve_strait/esi/ratelimit.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_ratelimit.py -v`
Expected: PASS (12 tests)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .github/workflows/ci.yml AGENTS.md src/eve_strait/esi/ratelimit.py tests/test_ratelimit.py
git commit -m "feat(esi): parse rate-limit headers, add unit test suite"
```

---

### Task 2: Governor state and pacing decisions

**Files:**
- Modify: `src/eve_strait/esi/ratelimit.py`
- Modify: `tests/test_ratelimit.py`

**Interfaces:**
- Consumes: `Limit`, `parse_limit`, `route_key` from Task 1.
- Produces: `Decision(action: str, seconds: float, reason: str)` where `action` is one of `"proceed"`, `"wait"`, `"decline"`; `RateLimitGovernor` with `observe(path, character_id, headers)`, `check(path, character_id, priority) -> Decision`, and `poll_interval(path, character_id, floor=30.0) -> float`. `priority` is `"interactive"` or `"background"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ratelimit.py`:

```python
from eve_strait.esi.ratelimit import Decision, RateLimitGovernor

ASSETS = "/characters/12345/assets/"


def gov_at(remaining, limit="1000/15m", clock=lambda: 1000.0):
    """A governor that has already seen one response for ASSETS."""
    g = RateLimitGovernor(clock=clock)
    g.observe(ASSETS, 12345, {
        "X-Ratelimit-Group": "assets",
        "X-Ratelimit-Limit": limit,
        "X-Ratelimit-Remaining": str(remaining),
    })
    return g


def test_unknown_route_proceeds_without_delay():
    g = RateLimitGovernor(clock=lambda: 1000.0)
    assert g.check(ASSETS, 12345, "background") == Decision("proceed")


def test_plenty_of_budget_proceeds():
    assert gov_at(800).check(ASSETS, 12345, "background").action == "proceed"


def test_below_half_paces_by_remaining_requests():
    # 400 tokens left = 200 requests at 2 tokens each, across a 900s window.
    d = gov_at(400).check(ASSETS, 12345, "interactive")
    assert d.action == "wait"
    assert d.seconds == pytest.approx(4.5)


def test_pacing_delay_is_capped_at_60s():
    d = gov_at(20).check(ASSETS, 12345, "interactive")
    assert d.seconds == 60.0


def test_below_reserve_floor_background_is_declined():
    d = gov_at(50).check(ASSETS, 12345, "background")   # 5% of 1000
    assert d.action == "decline"


def test_below_reserve_floor_interactive_still_spends():
    d = gov_at(50).check(ASSETS, 12345, "interactive")
    assert d.action == "wait"


def test_state_is_keyed_per_character():
    g = gov_at(50)
    # A different character is a different bucket and must be unaffected.
    assert g.check("/characters/999/assets/", 999, "background") == Decision("proceed")


def test_poll_interval_uses_floor_when_budget_is_generous():
    assert gov_at(1000).poll_interval(ASSETS, 12345) == 30.0


def test_poll_interval_derives_from_a_tight_limit():
    # 150 tokens: half to background = 75 tokens = 37.5 requests over 900s.
    g = gov_at(150, limit="150/15m")
    assert g.poll_interval(ASSETS, 12345) == pytest.approx(30.0)


def test_poll_interval_never_dips_below_the_floor():
    assert gov_at(5000, limit="5000/15m").poll_interval(ASSETS, 12345) == 30.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ratelimit.py -v`
Expected: FAIL with `ImportError: cannot import name 'Decision'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/eve_strait/esi/ratelimit.py`:

```python
import threading
import time

# Cost of a successful response, per CCP: 2XX = 2 tokens. Used to convert a
# remaining-token count into a remaining-request count.
TOKENS_PER_REQUEST = 2

PACE_BELOW = 0.50        # start spreading requests below half the budget
RESERVE_FLOOR = 0.10     # below this, only interactive work may spend
MAX_PACING_DELAY = 60.0  # never stall a single request longer than this
POLL_FLOOR_SECONDS = 30.0
BACKGROUND_SHARE = 0.50  # fraction of a group's budget timers may use


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
    def check(self, path: str, character_id: int | None,
              priority: str = "interactive") -> Decision:
        with self._lock:
            st = self._groups.get(self._key(path, character_id))
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ratelimit.py -v`
Expected: PASS (22 tests)

- [ ] **Step 5: Commit**

```bash
git add src/eve_strait/esi/ratelimit.py tests/test_ratelimit.py
git commit -m "feat(esi): pace requests as the rate-limit budget drains"
```

---

### Task 3: Parking on 429 and the error limit

**Files:**
- Modify: `src/eve_strait/esi/ratelimit.py`
- Modify: `tests/test_ratelimit.py`

**Interfaces:**
- Consumes: `RateLimitGovernor`, `Decision` from Task 2.
- Produces: `RateLimitGovernor.park(path, character_id, seconds)`; `RateLimitGovernor.observe_errors(headers)`; `MAX_INTERACTIVE_WAIT = 60.0`. `check()` gains park-aware behaviour: a parked group returns `decline` for background and `wait` for interactive, with `seconds` set to the remaining park time.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ratelimit.py`:

```python
class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_park_blocks_background_until_it_expires():
    clock = FakeClock()
    g = gov_at(1000, clock=clock)
    g.park(ASSETS, 12345, 30)
    assert g.check(ASSETS, 12345, "background").action == "decline"
    clock.t += 31
    assert g.check(ASSETS, 12345, "background").action == "proceed"


def test_short_park_makes_interactive_wait_the_remainder():
    clock = FakeClock()
    g = gov_at(1000, clock=clock)
    g.park(ASSETS, 12345, 30)
    clock.t += 10
    d = g.check(ASSETS, 12345, "interactive")
    assert d.action == "wait"
    assert d.seconds == pytest.approx(20.0)


def test_long_park_is_reported_not_slept_through():
    # 15 minutes is too long to freeze a button on. The transport turns a
    # wait longer than MAX_INTERACTIVE_WAIT into a RateLimited error.
    g = gov_at(1000)
    g.park(ASSETS, 12345, 900)
    d = g.check(ASSETS, 12345, "interactive")
    assert d.action == "wait"
    assert d.seconds > 60.0


def test_error_limit_parks_every_group():
    clock = FakeClock()
    g = gov_at(1000, clock=clock)
    g.observe_errors({"X-ESI-Error-Limit-Remain": "3",
                      "X-ESI-Error-Limit-Reset": "45"})
    assert g.check(ASSETS, 12345, "background").action == "decline"
    # A completely unrelated route is parked too: the error budget is global.
    assert g.check("/sovereignty/map/", None, "background").action == "decline"
    clock.t += 46
    assert g.check("/sovereignty/map/", None, "background").action == "proceed"


def test_healthy_error_budget_parks_nothing():
    g = gov_at(1000)
    g.observe_errors({"X-ESI-Error-Limit-Remain": "95",
                      "X-ESI-Error-Limit-Reset": "45"})
    assert g.check(ASSETS, 12345, "background").action == "proceed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ratelimit.py -v`
Expected: FAIL with `AttributeError: 'RateLimitGovernor' object has no attribute 'park'`

- [ ] **Step 3: Write minimal implementation**

In `src/eve_strait/esi/ratelimit.py`, add near the other constants:

```python
MAX_INTERACTIVE_WAIT = 60.0   # longer than this, tell the user instead of stalling
ERROR_LIMIT_FLOOR = 10        # X-ESI-Error-Limit-Remain below this parks everything
```

Add to `RateLimitGovernor.__init__`:

```python
        self._error_parked_until = 0.0
```

Add these methods:

```python
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
```

Replace the body of `check()` with this park-aware version (the pacing logic is unchanged, it just runs after the park checks):

```python
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
                return Decision("proceed")
            fraction = st.remaining / st.limit.max_tokens
            if fraction >= PACE_BELOW:
                return Decision("proceed")
            if fraction < RESERVE_FLOOR and priority == "background":
                return Decision("decline", reason="reserve floor")
            requests_left = max(st.remaining / TOKENS_PER_REQUEST, 1.0)
            delay = min(st.limit.window_seconds / requests_left, MAX_PACING_DELAY)
            return Decision("wait", delay, "pacing")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ratelimit.py -v`
Expected: PASS (27 tests)

- [ ] **Step 5: Commit**

```bash
git add src/eve_strait/esi/ratelimit.py tests/test_ratelimit.py
git commit -m "feat(esi): honour Retry-After parks and the ESI error limit"
```

---

### Task 4: The sqlite response cache

**Files:**
- Create: `src/eve_strait/esi/httpcache.py`
- Create: `tests/test_httpcache.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Freshness(fetched_at: float, expires_at: float)`; `Entry(body: bytes, etag: str, fetched_at: float, expires_at: float, headers: dict)`; `cache_key(method, url, params, character_id) -> str`; `parse_expires(headers, now) -> float | None`; `HttpCache(path, clock=time.time)` with `get(key) -> Entry | None`, `put(key, entry)`, `touch(key, expires_at)`, `status(key) -> Freshness | None`, `close()`.

**`Entry` stores the response headers, not just the body.** This is not decoration: `assets()` reads `X-Pages` off the response to know how many pages to walk. If a cached response returned no headers, `X-Pages` would default to `"1"` and a cache hit on page 1 would silently truncate a multi-page asset list to its first page — the worst kind of bug, because it looks like the character simply owns less.

`get()` returns the entry whether or not it has expired — the transport decides what to do with a stale entry, because a stale entry is still useful for an ETag revalidation and for serving to declined background callers.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_httpcache.py`:

```python
import pytest

from eve_strait.esi.httpcache import (Entry, HttpCache, cache_key,
                                      parse_expires)


@pytest.fixture
def cache(tmp_path):
    c = HttpCache(tmp_path / "test.sqlite")
    yield c
    c.close()


def entry(body=b'{"ok":true}', etag="W/\"abc\"", fetched=1000.0, expires=4600.0,
          headers=None):
    return Entry(body=body, etag=etag, fetched_at=fetched, expires_at=expires,
                 headers=headers if headers is not None else {"X-Pages": "3"})


def test_missing_key_returns_none(cache):
    assert cache.get("nope") is None


def test_roundtrip_preserves_body_and_etag(cache):
    cache.put("k", entry())
    got = cache.get("k")
    assert got.body == b'{"ok":true}'
    assert got.etag == 'W/"abc"'
    assert got.expires_at == 4600.0


def test_roundtrip_preserves_headers(cache):
    # X-Pages lives here. Losing it truncates paginated results on a cache
    # hit, which looks like missing assets rather than like a cache bug.
    cache.put("k", entry(headers={"X-Pages": "3", "last-modified": "yesterday"}))
    assert cache.get("k").headers["X-Pages"] == "3"


def test_put_overwrites_an_existing_key(cache):
    cache.put("k", entry(body=b"old"))
    cache.put("k", entry(body=b"new"))
    assert cache.get("k").body == b"new"


def test_expired_entries_are_still_returned(cache):
    # The transport needs the stale body: it carries the ETag for
    # revalidation and it is what a declined background call serves.
    cache.put("k", entry(expires=1.0))
    assert cache.get("k") is not None


def test_touch_extends_expiry_without_changing_the_body(cache):
    cache.put("k", entry())
    cache.touch("k", 9999.0)
    got = cache.get("k")
    assert got.expires_at == 9999.0
    assert got.body == b'{"ok":true}'


def test_status_reports_freshness_without_the_body(cache):
    cache.put("k", entry())
    st = cache.status("k")
    assert (st.fetched_at, st.expires_at) == (1000.0, 4600.0)


def test_status_of_unknown_key_is_none(cache):
    assert cache.status("nope") is None


def test_cache_survives_reopening(tmp_path):
    path = tmp_path / "p.sqlite"
    c1 = HttpCache(path)
    c1.put("k", entry())
    c1.close()
    c2 = HttpCache(path)
    assert c2.get("k").body == b'{"ok":true}'
    c2.close()


def test_keys_are_isolated_per_character():
    a = cache_key("GET", "https://esi/x/", {"page": 1}, 111)
    b = cache_key("GET", "https://esi/x/", {"page": 1}, 222)
    assert a != b


def test_key_ignores_param_ordering():
    a = cache_key("GET", "https://esi/x/", {"page": 1, "b": 2}, 111)
    b = cache_key("GET", "https://esi/x/", {"b": 2, "page": 1}, 111)
    assert a == b


def test_parse_expires_reads_an_http_date():
    headers = {"expires": "Wed, 19 Aug 2026 12:00:00 GMT"}
    assert parse_expires(headers, now=0.0) == pytest.approx(1786464000.0)


def test_parse_expires_returns_none_without_the_header():
    assert parse_expires({}, now=0.0) is None


def test_parse_expires_ignores_junk():
    assert parse_expires({"expires": "0"}, now=0.0) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_httpcache.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'eve_strait.esi.httpcache'`

- [ ] **Step 3: Write minimal implementation**

Create `src/eve_strait/esi/httpcache.py`:

```python
"""Disk cache for ESI responses, keyed by request identity.

sqlite rather than a file per entry: station and structure name resolution
can reach thousands of rows, and a cache directory with thousands of tiny
JSON files is hostile to both the filesystem and anyone reading it. sqlite3
is stdlib, so this adds no runtime dependency and survives the Nuitka build.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key         TEXT PRIMARY KEY,
    body        BLOB NOT NULL,
    etag        TEXT NOT NULL DEFAULT '',
    headers     TEXT NOT NULL DEFAULT '{}',
    fetched_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
)
"""


@dataclass(frozen=True)
class Freshness:
    fetched_at: float
    expires_at: float


@dataclass(frozen=True)
class Entry:
    body: bytes
    etag: str
    fetched_at: float
    expires_at: float
    headers: dict


def cache_key(method: str, url: str, params, character_id) -> str:
    """Stable key for one request identity.

    Hashed rather than stored raw: URLs with many params get long, and the
    key is a primary key we look up on every single call.
    """
    payload = json.dumps(
        [method.upper(), url, sorted((params or {}).items()), character_id],
        sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_expires(headers, now: float) -> float | None:
    """Absolute epoch time from an HTTP `expires` header. None if absent/junk.

    ESI sends a real HTTP date here; "0" and other cache-busting values are
    treated as absent rather than as "already expired", because the caller
    already handles a missing expiry.
    """
    raw = headers.get("expires") or headers.get("Expires")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError):
        return None


class HttpCache:
    """Thread-safe because Qt workers call ESI off the main thread."""

    def __init__(self, path: Path, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock:
            self._db.execute(_SCHEMA)
            self._db.commit()

    def get(self, key: str) -> Entry | None:
        """The stored entry, expired or not. Callers judge freshness."""
        with self._lock:
            row = self._db.execute(
                "SELECT body, etag, fetched_at, expires_at, headers FROM "
                "responses WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            headers = json.loads(row[4])
        except (TypeError, ValueError):
            headers = {}
        return Entry(row[0], row[1], row[2], row[3], headers)

    def put(self, key: str, entry: Entry) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO responses (key, body, etag, headers, fetched_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "body=excluded.body, etag=excluded.etag, "
                "headers=excluded.headers, "
                "fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
                (key, entry.body, entry.etag, json.dumps(dict(entry.headers)),
                 entry.fetched_at, entry.expires_at))
            self._db.commit()

    def touch(self, key: str, expires_at: float) -> None:
        """A 304 said the body is still good; just push the expiry out."""
        with self._lock:
            self._db.execute(
                "UPDATE responses SET expires_at = ?, fetched_at = ? WHERE key = ?",
                (expires_at, self._clock(), key))
            self._db.commit()

    def status(self, key: str) -> Freshness | None:
        with self._lock:
            row = self._db.execute(
                "SELECT fetched_at, expires_at FROM responses WHERE key = ?",
                (key,)).fetchone()
        return Freshness(row[0], row[1]) if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()
```

Note on `touch()`: it updates `fetched_at` as well as `expires_at`, because "as of 14:32" in the UI should mean "we confirmed this at 14:32", and a 304 is a confirmation.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_httpcache.py -v`
Expected: PASS (14 tests)

If `test_parse_expires_reads_an_http_date` fails on the exact epoch value, recompute it rather than adjusting the implementation: `python -c "from email.utils import parsedate_to_datetime as p; print(p('Wed, 19 Aug 2026 12:00:00 GMT').timestamp())"` and use that number. The assertion is checking that parsing happens, not that a particular date is special.

- [ ] **Step 5: Commit**

```bash
git add src/eve_strait/esi/httpcache.py tests/test_httpcache.py
git commit -m "feat(esi): sqlite response cache with ETag and expiry tracking"
```

---

### Task 5: The transport façade

**Files:**
- Create: `src/eve_strait/esi/transport.py`
- Create: `tests/test_transport.py`

**Interfaces:**
- Consumes: `RateLimitGovernor`, `Decision`, `MAX_INTERACTIVE_WAIT`, `route_key` (Task 3); `HttpCache`, `Entry`, `Freshness`, `cache_key`, `parse_expires` (Task 4); `RateLimited` from `esi/auth.py`.
- Produces: `Response` with `.json()`, `.headers`, `.status_code`, `.from_cache`, `.fetched_at`, `.expires_at`; `EsiTransport(session, cache, governor, clock, sleeper)` with `get(...)`, `post(...)`, `cache_status(...)`; module-level `get_transport() -> EsiTransport`; `CACHE_POLICY` override table; `NEVER = 0.0` and `PERMANENT = float("inf")` sentinels.

`get()` signature, which Task 6 depends on exactly:

```python
def get(self, path, *, params=None, character_id=None, headers=None,
        priority="interactive", force=False, timeout=30) -> Response
```

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transport.py`:

```python
import pytest

from eve_strait.esi.auth import RateLimited
from eve_strait.esi.httpcache import HttpCache
from eve_strait.esi.ratelimit import RateLimitGovernor
from eve_strait.esi.transport import EsiTransport


class FakeResponse:
    def __init__(self, status_code=200, body=b'{"v":1}', headers=None):
        self.status_code = status_code
        self.content = body
        self.headers = headers or {}

    def json(self):
        import json
        return json.loads(self.content)


class FakeSession:
    """Records requests and replays a scripted list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None,
                timeout=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "params": params,
                           "json": json})
        return self.responses.pop(0)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def make(tmp_path):
    def _make(responses, clock=None):
        clock = clock or FakeClock()
        slept = []
        t = EsiTransport(
            session=FakeSession(responses),
            cache=HttpCache(tmp_path / "t.sqlite", clock=clock),
            governor=RateLimitGovernor(clock=clock),
            clock=clock,
            sleeper=slept.append,
        )
        return t, t.session, slept, clock
    return _make


def fresh_headers(expires="Wed, 19 Aug 2026 12:00:00 GMT"):
    return {"expires": expires, "etag": 'W/"v1"',
            "X-Ratelimit-Group": "assets",
            "X-Ratelimit-Limit": "1000/15m",
            "X-Ratelimit-Remaining": "900"}


def test_first_call_hits_the_network_and_caches(make):
    t, session, _, _ = make([FakeResponse(headers=fresh_headers())])
    r = t.get("/characters/1/assets/", character_id=1)
    assert r.json() == {"v": 1}
    assert r.from_cache is False
    assert len(session.calls) == 1


def test_second_call_within_expiry_never_touches_the_network(make):
    t, session, _, _ = make([FakeResponse(headers=fresh_headers())])
    t.get("/characters/1/assets/", character_id=1)
    r = t.get("/characters/1/assets/", character_id=1)
    assert r.from_cache is True
    assert r.json() == {"v": 1}
    assert len(session.calls) == 1          # still one: the button was free


def test_cached_response_keeps_x_pages(make):
    # assets() walks pagination using this header. A cache hit that dropped
    # it would silently truncate a multi-page asset list to page 1.
    headers = dict(fresh_headers(), **{"X-Pages": "4"})
    t, _, _, _ = make([FakeResponse(headers=headers)])
    t.get("/characters/1/assets/", character_id=1, params={"page": 1})
    r = t.get("/characters/1/assets/", character_id=1, params={"page": 1})
    assert r.from_cache is True
    assert r.headers["X-Pages"] == "4"


def test_force_bypasses_a_fresh_entry(make):
    t, session, _, _ = make([FakeResponse(headers=fresh_headers()),
                             FakeResponse(body=b'{"v":2}',
                                          headers=fresh_headers())])
    t.get("/characters/1/assets/", character_id=1)
    r = t.get("/characters/1/assets/", character_id=1, force=True)
    assert r.json() == {"v": 2}
    assert len(session.calls) == 2


def test_expired_entry_revalidates_with_if_none_match(make):
    clock = FakeClock()
    t, session, _, _ = make([FakeResponse(headers=fresh_headers()),
                             FakeResponse(status_code=304, body=b"",
                                          headers=fresh_headers())], clock=clock)
    t.get("/characters/1/assets/", character_id=1)
    clock.t += 10_000_000                     # well past the expires header
    r = t.get("/characters/1/assets/", character_id=1)
    assert session.calls[1]["headers"]["If-None-Match"] == 'W/"v1"'
    assert r.json() == {"v": 1}               # stored body, not the empty 304
    assert r.from_cache is True


def test_never_cached_routes_always_hit_the_network(make):
    t, session, _, _ = make([FakeResponse(headers=fresh_headers()),
                             FakeResponse(headers=fresh_headers())])
    t.get("/characters/1/location/", character_id=1)
    t.get("/characters/1/location/", character_id=1)
    assert len(session.calls) == 2


def test_immutable_routes_are_cached_past_their_expires(make):
    clock = FakeClock()
    t, session, _, _ = make([FakeResponse(headers=fresh_headers())], clock=clock)
    t.get("/universe/stations/60003760/")
    clock.t += 10_000_000
    r = t.get("/universe/stations/60003760/")
    assert r.from_cache is True
    assert len(session.calls) == 1            # this is the dockables 429 fix


def test_429_parks_then_retries_once(make):
    t, session, slept, _ = make([
        FakeResponse(status_code=429, headers={"Retry-After": "5",
                                               "X-Ratelimit-Group": "assets"}),
        FakeResponse(headers=fresh_headers()),
    ])
    r = t.get("/characters/1/assets/", character_id=1)
    assert r.json() == {"v": 1}
    assert slept == [5.0]
    assert len(session.calls) == 2


def test_429_twice_raises_rate_limited(make):
    t, _, _, _ = make([
        FakeResponse(status_code=429, headers={"Retry-After": "5"}),
        FakeResponse(status_code=429, headers={"Retry-After": "5"}),
    ])
    with pytest.raises(RateLimited):
        t.get("/characters/1/assets/", character_id=1)


def test_a_long_retry_after_is_raised_not_slept_through(make):
    t, _, slept, _ = make([FakeResponse(status_code=429,
                                        headers={"Retry-After": "900"})])
    with pytest.raises(RateLimited):
        t.get("/characters/1/assets/", character_id=1)
    assert slept == []                        # never freeze a button for 15 min


def test_declined_background_call_serves_the_stale_body(make):
    clock = FakeClock()
    low = dict(fresh_headers(), **{"X-Ratelimit-Remaining": "10"})   # 1%
    t, session, _, _ = make([FakeResponse(headers=low)], clock=clock)
    t.get("/characters/1/assets/", character_id=1)
    clock.t += 10_000_000
    r = t.get("/characters/1/assets/", character_id=1, priority="background")
    assert r.from_cache is True
    assert len(session.calls) == 1


def test_declined_background_call_without_a_cache_entry_raises(make):
    t, _, _, _ = make([])
    t.governor.park("/characters/1/assets/", 1, 60)
    with pytest.raises(RateLimited):
        t.get("/characters/1/assets/", character_id=1, priority="background")


def test_cache_status_reports_without_a_request(make):
    t, session, _, _ = make([FakeResponse(headers=fresh_headers())])
    t.get("/characters/1/assets/", character_id=1)
    st = t.cache_status("/characters/1/assets/", character_id=1)
    assert st.expires_at > st.fetched_at
    assert len(session.calls) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transport.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'eve_strait.esi.transport'`

- [ ] **Step 3: Write minimal implementation**

Create `src/eve_strait/esi/transport.py`:

```python
"""The single path every ESI request takes: governed, cached, logged.

client.py talks to this and nothing else. Auth is deliberately not handled
here -- callers pass their own Authorization header -- so this module stays
independent of the token store and easy to test.
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from .. import config
from .auth import RateLimited
from .httpcache import Entry, HttpCache, cache_key, parse_expires
from .ratelimit import (MAX_INTERACTIVE_WAIT, RateLimitGovernor, route_key)

log = logging.getLogger(__name__)

NEVER = 0.0                  # do not cache this route at all
PERMANENT = float("inf")     # cache forever; the resource cannot change

# Overrides on top of ESI's own `expires` header. Only two reasons to appear
# here: the resource is genuinely immutable (longer), or the data is live by
# definition (never). Everything absent from this table uses `expires`.
# /universe/names/ is absent on purpose despite being immutable: it is a POST
# whose input is a JSON body, and cache_key hashes params, not bodies. Caching
# it would need body-aware keying, which is follow-up work; it already batches
# 1000 IDs per call, so it is not the burst that hurt us.
CACHE_POLICY: dict[str, float] = {
    "/universe/stations/{id}/": PERMANENT,
    "/universe/structures/{id}/": 7 * 24 * 3600,
    "/characters/{id}/location/": NEVER,
    "/characters/{id}/ship/": NEVER,
    "/characters/{id}/online/": NEVER,
}


class Response:
    """Uniform result whether it came from sqlite or the wire."""

    def __init__(self, body: bytes, headers, status_code: int,
                 from_cache: bool, fetched_at: float, expires_at: float):
        self.content = body
        self.headers = headers
        self.status_code = status_code
        self.from_cache = from_cache
        self.fetched_at = fetched_at
        self.expires_at = expires_at

    def json(self):
        import json
        return json.loads(self.content) if self.content else None


class EsiTransport:
    def __init__(self, session=None, cache=None, governor=None,
                 clock=time.time, sleeper=time.sleep):
        self.session = session or requests.Session()
        self.cache = cache or HttpCache(config.CACHE_DIR / "esi_cache.sqlite")
        self.governor = governor or RateLimitGovernor(clock=clock)
        self._clock = clock
        self._sleep = sleeper

    # -- policy -------------------------------------------------------------
    def _expiry_for(self, path: str, headers, now: float) -> float:
        override = CACHE_POLICY.get(route_key(path))
        if override is not None:
            return now + override if override != PERMANENT else PERMANENT
        return parse_expires(headers, now) or now

    def _cacheable(self, path: str) -> bool:
        return CACHE_POLICY.get(route_key(path), None) != NEVER

    # -- public -------------------------------------------------------------
    def cache_status(self, path, params=None, character_id=None):
        return self.cache.status(
            cache_key("GET", f"{config.ESI_BASE}{path}", params, character_id))

    def get(self, path, *, params=None, character_id=None, headers=None,
            priority="interactive", force=False, timeout=30) -> Response:
        url = f"{config.ESI_BASE}{path}"
        cacheable = self._cacheable(path)
        key = cache_key("GET", url, params, character_id)
        entry = self.cache.get(key) if cacheable else None
        now = self._clock()

        if entry and not force and entry.expires_at > now:
            return self._from_cache(entry)

        decision = self.governor.check(path, character_id, priority)
        if decision.action == "decline":
            if entry:
                log.debug("ESI budget low (%s); serving stale %s",
                          decision.reason, path)
                return self._from_cache(entry)
            raise RateLimited(
                "EVE's rate limit is nearly spent and there is no cached copy "
                "of this data yet. Try again shortly.")
        if decision.action == "wait":
            if decision.seconds > MAX_INTERACTIVE_WAIT:
                if entry:
                    return self._from_cache(entry)
                raise RateLimited(
                    f"EVE is rate-limiting this application for another "
                    f"{int(decision.seconds)} seconds.")
            self._sleep(decision.seconds)

        req_headers = dict(headers or {})
        if entry and entry.etag and not force:
            req_headers["If-None-Match"] = entry.etag

        resp = self.session.request("GET", url, headers=req_headers,
                                    params=params, timeout=timeout)
        self._observe(path, character_id, resp)

        if resp.status_code == 429:
            retry_after = _retry_after(resp.headers)
            self.governor.park(path, character_id, retry_after)
            if retry_after <= MAX_INTERACTIVE_WAIT and priority == "interactive":
                self._sleep(retry_after)
                resp = self.session.request("GET", url, headers=req_headers,
                                            params=params, timeout=timeout)
                self._observe(path, character_id, resp)
            if resp.status_code == 429:
                raise RateLimited(
                    "EVE is rate-limiting requests right now (HTTP 429). "
                    f"Retry in about {int(retry_after)} seconds.")

        now = self._clock()
        if resp.status_code == 304 and entry:
            expires_at = self._expiry_for(path, resp.headers, now)
            self.cache.touch(key, expires_at)
            # The stored headers, not the 304's: a 304 body is empty and its
            # headers omit X-Pages, which assets() needs to walk pagination.
            return Response(entry.body, entry.headers, 200, True, now, expires_at)

        resp.raise_for_status()
        expires_at = self._expiry_for(path, resp.headers, now)
        if cacheable:
            self.cache.put(key, Entry(body=resp.content,
                                      etag=resp.headers.get("etag", ""),
                                      fetched_at=now, expires_at=expires_at,
                                      headers=dict(resp.headers)))
        return Response(resp.content, resp.headers, resp.status_code,
                        False, now, expires_at)

    def _from_cache(self, entry) -> Response:
        return Response(entry.body, entry.headers, 200, True,
                        entry.fetched_at, entry.expires_at)

    def post(self, path, *, params=None, json=None, character_id=None,
             headers=None, timeout=30):
        """Never cached. Still governed: writes cost tokens too.

        Returns the raw requests.Response, because every POST caller here
        already handles it that way and none of them want caching.
        """
        url = f"{config.ESI_BASE}{path}"
        decision = self.governor.check(path, character_id, "interactive")
        if decision.action == "wait" and decision.seconds <= MAX_INTERACTIVE_WAIT:
            self._sleep(decision.seconds)
        resp = self.session.request("POST", url, headers=dict(headers or {}),
                                    params=params, json=json, timeout=timeout)
        self._observe(path, character_id, resp)
        return resp

    # -- internals ----------------------------------------------------------
    def _observe(self, path, character_id, resp) -> None:
        self.governor.observe(path, character_id, resp.headers)
        self.governor.observe_errors(resp.headers)
        limit = resp.headers.get("X-Ratelimit-Limit")
        if limit:
            log.debug("ESI %s group=%s limit=%s remaining=%s", path,
                      resp.headers.get("X-Ratelimit-Group"), limit,
                      resp.headers.get("X-Ratelimit-Remaining"))


def _retry_after(headers) -> float:
    try:
        return float(headers.get("Retry-After", 60))
    except (TypeError, ValueError):
        return 60.0


_transport = None
_transport_lock = threading.Lock()


def get_transport() -> EsiTransport:
    global _transport
    with _transport_lock:
        if _transport is None:
            _transport = EsiTransport()
        return _transport
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_transport.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS (54 tests)

- [ ] **Step 6: Commit**

```bash
git add src/eve_strait/esi/transport.py tests/test_transport.py
git commit -m "feat(esi): governed, cached transport for all ESI requests"
```

---

### Task 6: Route client.py through the transport

**Files:**
- Modify: `src/eve_strait/esi/client.py` (`EsiClient._get` at lines 276-285, `set_waypoint` at 330-345, and the six module-level functions at lines 44, 80, 127, 162, 186, 199, 222)
- Create: `tests/test_client_delegation.py`

**Interfaces:**
- Consumes: `get_transport()`, `Response` from Task 5.
- Produces: no new public names. `EsiClient._get` keeps its existing signature `(self, path, **params)` and still returns something with `.json()` and `.headers`, so its nine call sites are unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_client_delegation.py`:

```python
import types

from eve_strait.esi import client as client_mod


class StubTransport:
    def __init__(self):
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return types.SimpleNamespace(json=lambda: {"ok": True}, headers={},
                                     status_code=200, from_cache=False)


def make_client(monkeypatch):
    stub = StubTransport()
    monkeypatch.setattr(client_mod, "get_transport", lambda: stub)
    token = types.SimpleNamespace(access_token="tok", character_id=42,
                                  expired=False, character_name="Pilot")
    return client_mod.EsiClient(token, "client-id"), stub


def test_get_passes_character_id_for_bucket_keying(monkeypatch):
    c, stub = make_client(monkeypatch)
    c.location()
    path, kwargs = stub.calls[0]
    assert path == "/characters/42/location/"
    assert kwargs["character_id"] == 42


def test_get_passes_the_bearer_token(monkeypatch):
    c, stub = make_client(monkeypatch)
    c.location()
    _, kwargs = stub.calls[0]
    assert kwargs["headers"]["Authorization"] == "Bearer tok"


def test_priority_defaults_to_interactive(monkeypatch):
    c, stub = make_client(monkeypatch)
    c.location()
    _, kwargs = stub.calls[0]
    assert kwargs["priority"] == "interactive"


def test_background_priority_is_forwarded(monkeypatch):
    c, stub = make_client(monkeypatch)
    c.location(priority="background")
    _, kwargs = stub.calls[0]
    assert kwargs["priority"] == "background"


def test_module_level_calls_pass_no_character_id(monkeypatch):
    stub = StubTransport()
    monkeypatch.setattr(client_mod, "get_transport", lambda: stub)
    stub.get = lambda path, **kw: (stub.calls.append((path, kw)) or
                                   types.SimpleNamespace(
                                       json=lambda: [], headers={},
                                       status_code=200, from_cache=False))
    client_mod.incursions()
    path, kwargs = stub.calls[0]
    assert path == "/incursions/"
    assert kwargs.get("character_id") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_client_delegation.py -v`
Expected: FAIL with `AttributeError: module 'eve_strait.esi.client' has no attribute 'get_transport'`

- [ ] **Step 3: Rewrite `_get` and the auth plumbing**

In `src/eve_strait/esi/client.py`, add to the imports at the top:

```python
from .transport import get_transport
```

Replace `EsiClient.__init__` and `_get` (currently lines 259-285) with:

```python
    def __init__(self, token: auth.Token, client_id: str):
        self.token = token
        self.client_id = client_id
        # No private session any more: every ESI request in the app shares one
        # governed, cached transport so the rate-limit budget is tracked in
        # one place rather than per client instance.
        self.transport = get_transport()

    # -- auth plumbing ------------------------------------------------------
    def _ensure_token(self):
        # refresh_stored(), not refresh() + save(): several EsiClients can
        # hold copies of the same character's Token (MainWindow keeps one for
        # the active character while scan_cyno_alts builds one per character),
        # and SSO retires a refresh token as it rotates it.
        if self.token.expired:
            self.token = auth.refresh_stored(self.token, self.client_id)

    def _headers(self):
        return {"Authorization": f"Bearer {self.token.access_token}"}

    def _get(self, path: str, priority: str = "interactive",
             force: bool = False, **params):
        self._ensure_token()
        try:
            return self.transport.get(
                path, params=params or None,
                character_id=self.token.character_id,
                headers=self._headers(), priority=priority, force=force)
        except requests.HTTPError as exc:
            if getattr(exc.response, "status_code", None) != 401:
                raise
            # An access token can be rejected even when it looks unexpired.
            self.token = auth.refresh_stored(self.token, self.client_id)
            return self.transport.get(
                path, params=params or None,
                character_id=self.token.character_id,
                headers=self._headers(), priority=priority, force=force)
```

- [ ] **Step 4: Add priority pass-through to the live-location methods**

Replace `location`, `ship` and `online` (currently lines 302-328) so timers can mark themselves background. Keep the existing docstrings verbatim; only the signatures and the `_get` calls change:

```python
    def location(self, priority: str = "interactive") -> dict:
        cid = self.token.character_id
        return self._get(f"/characters/{cid}/location/", priority=priority).json()

    def ship(self, priority: str = "interactive") -> dict:
        cid = self.token.character_id
        return self._get(f"/characters/{cid}/ship/", priority=priority).json()

    def online(self, priority: str = "interactive") -> dict:
        cid = self.token.character_id
        try:
            return self._get(f"/characters/{cid}/online/", priority=priority).json()
        except requests.HTTPError:
            return {}
```

- [ ] **Step 5: Convert `set_waypoint` to the transport**

Replace the body of `set_waypoint` (currently lines 330-345) below the docstring with:

```python
        self._ensure_token()
        params = {
            "destination_id": destination_id,
            "add_to_beginning": str(add_to_beginning).lower(),
            "clear_other_waypoints": str(clear_other_waypoints).lower(),
        }
        resp = self.transport.post(
            "/ui/autopilot/waypoint/", params=params,
            character_id=self.token.character_id, headers=self._headers())
        check_response(resp, self.client_id)
```

- [ ] **Step 6: Convert the six module-level functions**

Each currently calls `requests.get(f"{config.ESI_BASE}/...", timeout=N)` followed by `check_response(resp)`. Replace each call, and delete the `check_response(resp)` line that follows it — the transport raises `RateLimited` on 429 and calls `raise_for_status()` itself.

Keep every surrounding `try/except (requests.RequestException, ValueError)` block exactly as it is. Those are the "intel data must never block routing" guards and they still catch what the transport raises.

`priority="background"` on all of these: they are the timed intel refreshes, and they are exactly the work that should yield when the budget is thin.

Line 54, in `sovereignty`:

```python
        resp = get_transport().get("/sovereignty/map/", timeout=45,
                                   priority="background")
        rows = resp.json()
```

Line 95, in `system_activity`:

```python
        r = get_transport().get("/universe/system_kills/", timeout=45,
                                priority="background")
```

Line 109, in `system_activity`:

```python
        r = get_transport().get("/universe/system_jumps/", timeout=45,
                                priority="background")
```

Line 140, in `sovereignty_defense`:

```python
        r = get_transport().get("/sovereignty/structures/", timeout=45,
                                priority="background")
```

Line 172, in `industry_indices`:

```python
        r = get_transport().get("/industry/systems/", timeout=45,
                                priority="background")
```

Line 189, in `incursions`:

```python
        resp = get_transport().get("/incursions/", timeout=20,
                                   priority="background")
```

In `system_activity`, the line `out["expires"] = r.headers.get("expires", "")` still works: the transport's `Response` exposes `.headers`, and a cached response now carries the stored headers.

Line 209, in `resolve_ids`:

```python
        resp = get_transport().post("/universe/ids/", json=names, timeout=30)
        data = resp.json()
```

Line 228, in `resolve_names`:

```python
        resp = get_transport().post("/universe/names/", json=ids, timeout=20)
        return {row["id"]: row["name"] for row in resp.json()}
```

Both keep their existing `try/except` wrappers and both drop their `check_response(resp)` line. `post()` returns the raw `requests.Response`, so `.json()` behaves exactly as before. Neither is cached — see the comment above `CACHE_POLICY` for why name resolution needs body-aware keying before it can be.

`resolve_names` posts to `/universe/names/`, so it uses `get_transport().post(...)`. Its results are the permanent-cache case, but POST is not cached by the transport — leave the existing in-function behaviour alone; caching name resolution is out of scope for this task and `resolve_names` already batches.

- [ ] **Step 7: Verify no direct ESI calls remain**

Run: `grep -n "requests\.\(get\|post\)" src/eve_strait/esi/client.py`
Expected: no output. If any line prints, it was missed above.

- [ ] **Step 8: Run the tests**

Run: `uv run pytest -q`
Expected: PASS (59 tests)

- [ ] **Step 9: Verify the app still runs**

Run: `uv run eve-strait`

Check by hand: the map loads, and if you have a token linked, the character panel populates. Close the app.

- [ ] **Step 10: Commit**

```bash
git add src/eve_strait/esi/client.py tests/test_client_delegation.py
git commit -m "refactor(esi): route every ESI call through the governed transport"
```

---

### Task 7: Paginated assets with a consistency check

**Files:**
- Modify: `src/eve_strait/esi/client.py` (`assets`, currently lines 287-300)
- Create: `tests/test_assets_paging.py`

**Interfaces:**
- Consumes: `EsiClient._get` from Task 6.
- Produces: `EsiClient.assets(force: bool = False) -> list[dict]`; raises `AssetsChangedDuringFetch` (new, defined in `client.py`) when pages disagree.

- [ ] **Step 1: Write the failing test**

Create `tests/test_assets_paging.py`:

```python
import types

import pytest

from eve_strait.esi import client as client_mod


class PagedTransport:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append(kwargs.get("params"))
        page = (kwargs.get("params") or {}).get("page", 1)
        body, last_modified = self.pages[page - 1]
        return types.SimpleNamespace(
            json=lambda: body, status_code=200, from_cache=False,
            headers={"X-Pages": str(len(self.pages)),
                     "last-modified": last_modified})


def make_client(monkeypatch, pages):
    stub = PagedTransport(pages)
    monkeypatch.setattr(client_mod, "get_transport", lambda: stub)
    token = types.SimpleNamespace(access_token="tok", character_id=42,
                                  expired=False, character_name="Pilot")
    return client_mod.EsiClient(token, "cid"), stub


STAMP = "Wed, 19 Aug 2026 12:00:00 GMT"
LATER = "Wed, 19 Aug 2026 13:00:00 GMT"


def test_all_pages_are_concatenated(monkeypatch):
    c, _ = make_client(monkeypatch, [([{"a": 1}], STAMP), ([{"b": 2}], STAMP)])
    assert c.assets() == [{"a": 1}, {"b": 2}]


def test_pages_that_disagree_are_rejected(monkeypatch):
    # CCP's advice: if last-modified shifts mid-walk the data refreshed
    # underneath us and the concatenation is a torn read.
    c, _ = make_client(monkeypatch, [([{"a": 1}], STAMP), ([{"b": 2}], LATER)])
    with pytest.raises(client_mod.AssetsChangedDuringFetch):
        c.assets()


def test_single_page_needs_no_stamp(monkeypatch):
    c, _ = make_client(monkeypatch, [([{"a": 1}], "")])
    assert c.assets() == [{"a": 1}]


def test_force_is_forwarded_to_every_page(monkeypatch):
    c, stub = make_client(monkeypatch, [([{"a": 1}], STAMP), ([{"b": 2}], STAMP)])
    captured = []
    original = stub.get

    def spy(path, **kwargs):
        captured.append(kwargs.get("force"))
        return original(path, **kwargs)

    stub.get = spy
    c.assets(force=True)
    assert captured == [True, True]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_assets_paging.py -v`
Expected: FAIL with `AttributeError: module 'eve_strait.esi.client' has no attribute 'AssetsChangedDuringFetch'`

- [ ] **Step 3: Write minimal implementation**

In `src/eve_strait/esi/client.py`, add near the top after the imports:

```python
class AssetsChangedDuringFetch(RuntimeError):
    """The asset list refreshed while we were walking its pages.

    Concatenating pages either side of a refresh produces a list that never
    existed: items can appear twice or vanish. Better to say so and let the
    caller retry than to cache a torn read for an hour.
    """
```

Replace `assets` (lines 287-300) with:

```python
    def assets(self, force: bool = False) -> list[dict]:
        cid = self.token.character_id
        out: list[dict] = []
        page = 1
        stamp = None
        while True:
            resp = self._get(f"/characters/{cid}/assets/", page=page, force=force)
            page_stamp = resp.headers.get("last-modified", "")
            if stamp is None:
                stamp = page_stamp
            elif page_stamp and page_stamp != stamp:
                raise AssetsChangedDuringFetch(
                    "Your asset list changed while it was being read. "
                    "Try the scan again.")
            out.extend(resp.json())
            pages = int(resp.headers.get("X-Pages", "1"))
            if page >= pages:
                break
            page += 1
        return out
```

- [ ] **Step 4: Handle the new exception where assets are scanned**

In `scan_cyno_alts` (line 609), the existing `except Exception as exc` already catches `AssetsChangedDuringFetch` and records it as a per-character note, which is the right behaviour — one alt's torn read must not abort the whole roll-call. Add the message-clarifying clause before the generic handler:

```python
        except AssetsChangedDuringFetch:
            notes.append(f"{name}: assets changed mid-read; scan again to "
                         "include this character.")
            continue
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest -q`
Expected: PASS (63 tests)

- [ ] **Step 6: Commit**

```bash
git add src/eve_strait/esi/client.py tests/test_assets_paging.py
git commit -m "feat(esi): reject torn asset reads across paginated fetches"
```

---

### Task 8: Freshness label and force refresh in the UI

**Files:**
- Modify: `src/eve_strait/ui/panels/character_panel.py` (cyno section, lines 165-191)
- Modify: `src/eve_strait/ui/main_window.py` (`_scan_cyno_alts`, lines 2592-2613)

**Interfaces:**
- Consumes: `EsiTransport.cache_status` (Task 5), `Freshness` (Task 4).
- Produces: `CharacterPanel.set_cyno_freshness(fetched_at: float | None, expires_at: float | None)`; `CharacterPanel.force_cyno_requested` signal; `MainWindow._scan_cyno_alts(force: bool = False)`.

No unit tests: this is Qt widget wiring, which the project verifies by running the app.

- [ ] **Step 1: Add the freshness label and the force-refresh action**

In `src/eve_strait/ui/panels/character_panel.py`, add next to the existing `scan_cyno_requested` signal at line 35:

```python
    force_cyno_requested = Signal()
```

After `self.sec_cyno.add(self.btn_cyno)` (line 174), insert:

```python
        # Asset data is cached for as long as ESI says it is valid (about an
        # hour). Saying so turns "the button did nothing" into "the button
        # correctly did nothing", which is the difference between a bug
        # report and an informed user.
        self.lbl_cyno_age = QLabel("")
        self.lbl_cyno_age.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        self.sec_cyno.add(self.lbl_cyno_age)

        # Force refresh is a menu action, not a second button: it spends the
        # rate-limit budget the cache exists to protect, so it should take
        # deliberate effort to reach.
        self.btn_cyno.setContextMenuPolicy(Qt.ActionsContextMenu)
        act_force = QAction("Force refresh (ignores cache)", self.btn_cyno)
        act_force.triggered.connect(self.force_cyno_requested)
        self.btn_cyno.addAction(act_force)
```

Add the imports this needs at the top of the file: `QAction` from `PySide6.QtGui`, and `Qt` from `PySide6.QtCore` if not already imported.

- [ ] **Step 2: Add the label updater**

In the same file, next to `set_cyno_scanning` (line 190):

```python
    def set_cyno_freshness(self, fetched_at, expires_at):
        """Show when the cached asset data was read and when it expires."""
        if not fetched_at:
            self.lbl_cyno_age.setText("")
            return
        import time
        when = time.strftime("%H:%M", time.localtime(fetched_at))
        remaining = (expires_at or 0) - time.time()
        if remaining > 60:
            self.lbl_cyno_age.setText(
                f"as of {when} · refreshes in {int(remaining // 60)} min")
        else:
            self.lbl_cyno_age.setText(f"as of {when} · ready to refresh")
```

- [ ] **Step 3: Wire the signal and the force flag**

In `src/eve_strait/ui/main_window.py`, next to the existing connection at line 1699:

```python
        self.character.force_cyno_requested.connect(
            lambda: self._scan_cyno_alts(force=True))
```

Change the signature at line 2592 to `def _scan_cyno_alts(self, force: bool = False):` and pass the flag through the worker lambda:

```python
        w = Worker(lambda progress=None: _client.scan_cyno_alts(
            tokens, cid, mods, progress, force=force))
```

- [ ] **Step 4: Thread `force` through `scan_cyno_alts`**

In `src/eve_strait/esi/client.py`, change the signature at line 609 to:

```python
def scan_cyno_alts(tokens, client_id: str, cyno_modules: dict[int, str],
                   progress=None, force: bool = False) -> tuple[list, list[str]]:
```

and inside the loop change the three calls to:

```python
            location = c.location()
            ship = c.ship()
            assets = c.assets(force=force)
```

`location` and `ship` are never cached, so `force` does not apply to them.

- [ ] **Step 5: Update the label after a scan**

In `_on_cyno_alts` (line 2615), after `self.character.set_cyno_scanning(False)`:

```python
        from ..esi.transport import get_transport
        if self.token:
            st = get_transport().cache_status(
                f"/characters/{self.token.character_id}/assets/",
                params={"page": 1}, character_id=self.token.character_id)
            self.character.set_cyno_freshness(
                st.fetched_at if st else None, st.expires_at if st else None)
```

- [ ] **Step 6: Verify by hand**

Run: `uv run eve-strait`

Check, with a character linked:
1. Click "Scan my characters". It completes and the label reads "as of HH:MM · refreshes in N min".
2. Click it again immediately. It returns effectively instantly and the label is unchanged — no network traffic was spent.
3. Right-click the button, choose "Force refresh (ignores cache)". It re-fetches and the label's timestamp updates.

- [ ] **Step 7: Commit**

```bash
git add src/eve_strait/ui/panels/character_panel.py src/eve_strait/ui/main_window.py src/eve_strait/esi/client.py
git commit -m "feat(ui): show cyno asset cache age and offer a force refresh"
```

---

### Task 9: Derive the location polling cadence from the budget

This is the task that addresses the suspected root cause: 15s polling consumes 120 tokens per 15-minute window, which is ~80% of a 150/15m bucket before any other work happens.

**Files:**
- Modify: `src/eve_strait/ui/main_window.py` (lines 466-489, `LOCATION_POLL_MS` and `_sync_location_tracking`)
- Modify: `tests/test_ratelimit.py`

**Interfaces:**
- Consumes: `RateLimitGovernor.poll_interval` (Task 2), `get_transport()` (Task 5), `EsiClient.location(priority=...)` (Task 6).
- Produces: no new public names.

- [ ] **Step 1: Write the failing test for the interval used by the UI**

Append to `tests/test_ratelimit.py`:

```python
def test_poll_interval_respects_a_custom_floor():
    g = gov_at(150, limit="150/15m")
    assert g.poll_interval(ASSETS, 12345, floor=5.0) == pytest.approx(24.0)


def test_poll_interval_for_an_unobserved_route_is_the_floor():
    g = RateLimitGovernor(clock=lambda: 1000.0)
    assert g.poll_interval("/characters/1/location/", 1) == 30.0
```

- [ ] **Step 2: Run tests to verify they fail or pass**

Run: `uv run pytest tests/test_ratelimit.py -v`
Expected: PASS. `poll_interval` was built in Task 2; these two cases pin the behaviour the UI now depends on. If they fail, fix `poll_interval` before continuing — the UI change below is meaningless on a broken interval.

- [ ] **Step 3: Replace the hardcoded interval**

In `src/eve_strait/ui/main_window.py`, replace the comment block and constant at lines 466-472 with:

```python
    # Live location is only worth polling ESI for while something actually
    # wants it -- right now that is only the auto-waypoint feature.
    #
    # The interval is derived from the rate-limit headers rather than fixed:
    # /characters/{id}/location/ sits in the character-location group, and at
    # the old fixed 15s this poll alone burned 120 tokens per 15-minute
    # window -- around 80% of a 150/15m bucket -- so a cyno scan touching the
    # same bucket would tip it into a 429. The governor gives background work
    # half the group's budget; the 30s floor keeps us above the endpoint's own
    # server-side cache, below which polling learns nothing new anyway.
    LOCATION_POLL_FLOOR_MS = 30_000

    def _location_poll_ms(self) -> int:
        if not self.token:
            return self.LOCATION_POLL_FLOOR_MS
        from ..esi.transport import get_transport
        seconds = get_transport().governor.poll_interval(
            f"/characters/{self.token.character_id}/location/",
            self.token.character_id,
            floor=self.LOCATION_POLL_FLOOR_MS / 1000)
        return int(seconds * 1000)
```

- [ ] **Step 4: Use it in `_sync_location_tracking`**

Replace the `self._location_timer.start(self.LOCATION_POLL_MS)` line (line 484) with:

```python
                self._location_timer.start(self._location_poll_ms())
```

And, so the interval tracks a limit we learn about only after the first response, re-arm after each poll. At the end of `_fetch_location`'s success path, add:

```python
        # Re-arm from the current budget: the first poll is what teaches the
        # governor this group's real limit.
        if getattr(self, "_location_timer", None) is not None and \
                self._location_timer.isActive():
            self._location_timer.setInterval(self._location_poll_ms())
```

- [ ] **Step 5: Mark the poll as background work**

Find the `location()` call inside `_fetch_location` and change it to `location(priority="background")`. This is what lets the governor decline the poll — and the UI keep its last known position — when the budget is nearly spent.

Run `grep -n "\.location()" src/eve_strait/ui/main_window.py` to find every call site and change only the one inside `_fetch_location`. Calls made in response to a button press stay interactive.

- [ ] **Step 6: Stagger the intel refresh timer**

CCP's guidance is to schedule periodic work from the end of the last run rather than on a fixed wall-clock interval, so that every client running this app does not fire in lockstep on the hour.

`_start_intel_timer` (line 516) currently uses a repeating `QTimer` on a fixed interval. Change it to a single-shot timer that re-arms when the refresh finishes, with a small random offset. Replace the method with:

```python
    def _start_intel_timer(self):
        """Re-poll on the configured interval. 0 minutes means never.

        Single-shot and re-armed on completion rather than a repeating timer:
        CCP asks that periodic jobs schedule from the end of the last run,
        with some spread, so that every copy of an app does not stampede the
        same endpoints at the same moment.
        """
        from PySide6.QtCore import QTimer
        if getattr(self, "_intel_timer", None) is None:
            self._intel_timer = QTimer(self)
            self._intel_timer.setSingleShot(True)
            self._intel_timer.timeout.connect(self._run_intel_refresh)
        self._arm_intel_timer()

    def _arm_intel_timer(self):
        import random
        minutes = config.get_intel_refresh_minutes()
        if minutes <= 0:
            self._intel_timer.stop()
            return
        base = minutes * 60 * 1000
        self._intel_timer.start(base + random.randint(0, 60_000))

    def _run_intel_refresh(self):
        try:
            self.refresh_intel()
        finally:
            self._arm_intel_timer()
```

The `try/finally` matters: a failed refresh must still re-arm, or one transient network error silently stops intel updating for the rest of the session.

- [ ] **Step 7: Verify by hand**

Run: `uv run eve-strait`

Enable auto-waypoint with a character linked, then confirm the location still updates and the app does not stall. The poll is now 30s, so allow up to a minute to see a change after undocking or jumping.

Then open the intel settings, set the refresh interval to 1 minute, and confirm intel still refreshes roughly every 1-2 minutes and keeps refreshing (proving the re-arm works).

- [ ] **Step 8: Run the suite and commit**

Run: `uv run pytest -q`
Expected: PASS (65 tests)

```bash
git add src/eve_strait/ui/main_window.py tests/test_ratelimit.py
git commit -m "fix(esi): derive poll cadence from budget, stagger intel refresh"
```

---

### Task 10: Confirm the limits against live ESI, then document

The plan so far rests on an inference: that the `character-location` group's limit is near 150/15m. That number came from CCP's blog describing limits set around the 99th percentile of observed usage, not from this application's own traffic. If the real limit is 1000/15m, the polling change was unnecessary and the asset cache was the whole fix. Either way we should know.

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-19-esi-rate-limiting-design.md`

**Interfaces:**
- Consumes: the debug logging added in `EsiTransport._observe` (Task 5).
- Produces: no code.

- [ ] **Step 1: Capture real rate-limit headers**

Run the app with debug logging on:

```bash
uv run python -c "import logging; logging.basicConfig(level=logging.DEBUG); from eve_strait.__main__ import main; main()"
```

With a character linked, do all three: let location polling run for two minutes, run a cyno scan, and refresh dockables. Record from the log output, for each distinct `group=`, the observed `limit=` value.

- [ ] **Step 2: Record the findings in the spec**

Add a section to `docs/superpowers/specs/2026-08-19-esi-rate-limiting-design.md` headed "Observed limits", followed by the date you took the measurement, with a table of three columns: group name, the `X-Ratelimit-Limit` value seen, and which flow hit it. State plainly whether the 150/15m assumption behind the polling change held.

If the observed `character-location` limit is far above 150/15m, note that the 30s floor is now conservative rather than necessary, and that it could be revisited — but do not change it in this task. A cadence change deserves its own measurement.

- [ ] **Step 3: Document the caching behaviour for users**

In `README.md`, add to the ESI section a short paragraph:

```markdown
Eve-Strait caches ESI responses for exactly as long as EVE says they are
valid — about an hour for asset data — and paces its own requests against
EVE's published rate limits. That means pressing "Scan my characters" twice
in a row is free the second time: the panel shows when the data was read and
when it next refreshes. Right-click the button for a forced refresh if you
have just refitted, though EVE's own copy may still be up to an hour behind.
```

- [ ] **Step 4: Full verification**

Run: `uv run pytest -q` — expected PASS.
Run: `python -m compileall -q src` — expected silent.
Run: `uv run eve-strait` — the app starts, the map loads, a linked character populates.

Confirm `esi_cache.sqlite` now exists in the cache directory:

```bash
ls "$LOCALAPPDATA/eve-strait/cache/esi_cache.sqlite"
```

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/specs/2026-08-19-esi-rate-limiting-design.md
git commit -m "docs: record observed ESI rate limits and cache behaviour"
```

---

## Verification checklist

- [ ] `uv run pytest -q` passes (65 tests).
- [ ] `grep -n "requests\.\(get\|post\)" src/eve_strait/esi/client.py` returns nothing.
- [ ] A second cyno scan within the hour makes no network requests.
- [ ] A second dockables refresh resolves station names from sqlite, not the network.
- [ ] Location polling runs no faster than every 30s.
- [ ] The app still starts and routes with no ESI login at all.
