# ESI Rate Limiting, Caching and Polling Cadence

**Date:** 2026-08-19
**Status:** Approved design, ready for implementation planning

## Problem

The app has hit HTTP 429 from ESI in two flows:

1. **Cyno alt scan** (`scan_cyno_alts`) - loops every linked character calling
   `location()`, `ship()` and the full paginated `assets()`.
2. **First login / dockables refresh** (`dockable_locations()`) - resolves many
   station and structure IDs in a tight loop.

The current code is reactive only. `check_response()` in `esi/auth.py` raises
`RateLimited` on 420/429 and otherwise ignores every rate-limit signal CCP
sends. Nothing reads `Retry-After`, nothing reads `X-Ratelimit-*`, and nothing
honours the `expires` header. Asset data is re-fetched in full on every scan.

### Suspected root cause of the cyno-scan 429

`LOCATION_POLL_MS = 15_000` (`ui/main_window.py`) polls `/characters/{id}/location/`
four times a minute: 60 requests per 15-minute window, 120 tokens. The
`character-location` group limit can be as low as 150/15m, so idle polling alone
can consume ~80% of that bucket. A cyno scan then calls `location()` and `ship()`
for the same character - same group, same bucket - and tips it over.

No amount of asset caching fixes this. The polling cadence itself must change.

## How ESI rate limiting actually works

Confirmed against CCP documentation (see References):

- **Floating 15-minute window.** Tokens consumed by a request return to the
  bucket one window later.
- **Token costs by status:** 2XX = 2, 3XX = 1, 4XX = 5, 5XX = 0. Conditional
  requests that return 304 therefore cost half of a normal fetch, and error
  storms are the expensive failure mode.
- **Bucket keying:** authenticated routes bucket per `applicationID:characterID`;
  unauthenticated routes per `sourceIP` or `sourceIP:applicationID`. Per-alt
  scans spread across separate buckets; unauthenticated name and station
  resolution all lands in one shared bucket.
- **Headers:** `X-Ratelimit-Group`, `X-Ratelimit-Limit` (e.g. `150/15m`),
  `X-Ratelimit-Remaining`, `X-Ratelimit-Used`, and `Retry-After` on 429.
- **Observed limits** sit around 1000-1800 tokens/15m for most groups, roughly
  one request every two seconds sustained.
- Requesting before the `expires` header is explicitly called out as
  cache-circumvention and can get an application banned.

## Decisions

| Decision | Choice |
|---|---|
| Cache lifetime | Driven by ESI's own `expires` header, with per-route overrides |
| Full asset scans | Button-triggered only, never on a timer |
| Button press on fresh data | Serves cache, shows "as of HH:MM - refreshes in N min" |
| Escape hatch | Explicit "Force refresh" menu item, never the primary click |
| Storage | stdlib `sqlite3`, single file in `CACHE_DIR` |
| Background vs interactive | Background declines when budget is low; interactive waits |

Caching assets for "about an hour" is not a number this project invents and
maintains: `/characters/{id}/assets/` already carries a 3600-second `expires`.
Honouring the header produces the desired TTL for free and is also the
compliance story.

## Scope

**In scope:** `esi/client.py` (9 `EsiClient._get` call sites, 6 module-level
`requests.get` calls) and the `esi/auth.py` error path.

**Out of scope - not ESI, not in any ESI rate-limit group:**

- `data/universe.py` - Fuzzwork SDE downloads
- `esi/images.py` - `images.evetech.net`, already disk-cached
- `esi/evescout.py`, `esi/wanderer.py`, `esi/zkill.py` - third-party services

## Architecture

One new module, `src/eve_strait/esi/transport.py`, holding a process-wide
singleton with three collaborators behind one facade.

### HttpCache

sqlite at `CACHE_DIR/esi_cache.sqlite`. Rows keyed by
`(method, url, sorted params, character_id)`. Stores body, ETag, `fetched_at`,
`expires_at`. Character-keyed so two linked alts never read each other's assets.

sqlite rather than JSON files because station and structure resolution can reach
thousands of entries; a file per entry would litter the cache directory. It is
stdlib, so no new runtime dependency, and it survives the Nuitka build.

### RateLimitGovernor

Per-group token state parsed from `X-Ratelimit-*`, plus a "parked until" clock
per group set by `Retry-After`. State is keyed by
`(X-Ratelimit-Group, character_id or "anon")`, matching CCP's bucket keying.
Limits are learned from responses; before the first response the governor starts
optimistic.

Thread safety is mandatory, not optional: Qt workers already call ESI off the
main thread and `scan_cyno_alts` builds a client per character, so the governor
and cache are shared mutable state from day one. One lock each.

### EsiTransport

The only thing `client.py` talks to.

```python
transport.get(path, *, params=None, character_id=None,
              token=None, force=False, cacheable=True) -> Response
transport.post(path, ...)          # governed, never cached
transport.cache_status(path, character_id) -> Freshness | None
```

`Response` carries `.json()`, `.headers`, `.status_code`, plus `.from_cache`,
`.fetched_at` and `.expires_at`. `Freshness` is what the countdown label reads;
it answers "when was this fetched, when does it expire" without a request.

## Data flow (GET)

1. Build the cache key. Look up the cache.
2. **Fresh and not `force`** - return immediately. Zero tokens, no network.
   This is the button-press-on-fresh-data case.
3. Otherwise ask the governor for permission. It blocks if the group is parked
   or the budget is nearly spent.
4. Issue the request. If an expired entry with an ETag is held, send
   `If-None-Match`.
5. **304** - bump the stored `expires_at`, return the stored body. 1 token.
6. **2XX** - store the body with `expires_at` from the override table, else from
   the `expires` header.
7. **429** - feed `Retry-After` to the governor, park the group, retry once. If
   it fails again, raise `RateLimited`.
8. Update governor state from the response headers either way.

Step 2 fixes the dockables 429 outright: `/universe/stations/{id}/` is
immutable, so after the first run that entire resolution loop is served from
sqlite and never touches the network again.

## Cache policy overrides

Default is the `expires` header. Overrides exist only where the resource is
genuinely immutable (longer) or where we choose to be stingier than CCP.

| Route | Policy | Rationale |
|---|---|---|
| `/universe/stations/{id}/`, `/universe/names/` | permanent | Immutable. Kills the loop that caused 429 #2. |
| `/universe/structures/{id}/` | 7 days | Name changes only on ownership transfer. |
| assets, contacts, corp structures, starbases | `expires` + ETag | ~1h falls out of the header. |
| `/characters/{id}/location/`, `/ship/`, `/online/` | never cached | Live data by definition. |

## Pacing

Two priorities:

- **`interactive`** - button presses, force refresh, `set_waypoint`. Waits when
  throttled and reports progress.
- **`background`** - timers. Never waits; if the budget is low it declines and
  the caller serves stale data.

Rules:

- Above 50% remaining: no delay. Bursting is explicitly permitted by CCP.
- Below 50%: insert a delay of `window_seconds / (remaining_tokens / 2)` before
  each request, capped at 60s. The division by 2 converts remaining *tokens*
  into remaining *requests* at the 2XX cost, which is what we are actually
  spreading across the rest of the window.
- Below a **10% reserve floor**: background requests are refused; only
  `interactive` may spend. The floor guarantees a waypoint set or a cyno scan
  still works after a timer has been grinding all afternoon.
- **429** parks the whole group until `now + Retry-After`. Background returns
  stale immediately. Interactive waits on the park **only if `Retry-After` is
  60s or less**; a longer park raises `RateLimited` naming the wait, because
  silently freezing a button for several minutes is worse than saying so.

**Before any header is seen**, the governor starts optimistic and applies no
delay. It has no limit to divide by, and guessing a wrong limit would either
throttle needlessly or provide false comfort. Polling cadence in that state uses
the 30s floor described below.

### Error limiting

Distinct from token limiting and tracked separately: `X-ESI-Error-Limit-Remain`
and `X-ESI-Error-Limit-Reset` are a fixed-window error budget, and exhausting it
causes ESI to discard all requests until the window ends. Because 4XX responses
also cost 5 tokens each, an error storm drains both budgets at once. When
`X-ESI-Error-Limit-Remain` drops below 10, the governor parks **all** groups
until `X-ESI-Error-Limit-Reset` seconds have passed.

## Polling cadence

Replace the hardcoded `LOCATION_POLL_MS = 15_000` with a cadence derived from
observed headers: background polling gets 50% of the group's tokens and the
interval follows from the observed limit, with a **30s floor**. Worked example
on a 150/15m group: 75 tokens available to background, at 2 tokens per request
that is ~37 requests across 900s, or one every ~24s - so the 30s floor governs.
On a 1000/15m group the derived interval is well under the floor, and the floor
governs again; the derivation only starts to matter if CCP tightens limits
below today's values. Until the first header arrives, the floor is used.

The existing rationale that the endpoint is server-side cached for a few seconds
remains true and still bounds the useful floor.

Timed refreshes reschedule from job completion plus jitter rather than on a
fixed wall-clock interval, per CCP's guidance against `*/5`-style scheduling.

## Integration

**`client.py`** - `_get` becomes a delegation to `transport.get(...)` passing
`character_id` and the token; the 401 refresh-and-retry stays where it is. The
six module-level functions pass `character_id=None`. `assets()` caches per page
and checks `last-modified` consistency across pages per CCP's pagination advice;
an inconsistent set is discarded rather than cached half-torn.

**UI** - `cache_status()` feeds an "as of 14:32 - refreshes in 23 min" label
next to the cyno-scan and dockables buttons. Force refresh is a menu item, not
the primary click, so it cannot be hit by reflex.

**Errors** - `RateLimited` survives unchanged as the interactive-path escape;
its shared-client-ID guidance remains correct. Background callers never raise
into the UI.

## Testing

This departs from the existing convention in AGENTS.md, which states there is no
committed test suite and that CI only byte-compiles and imports the package.

The governor is clock arithmetic and the cache is expiry logic. These are
exactly the places where a silent off-by-one means either renewed 429s or
serving hour-old data forever, and neither is visible by running the app. This
work adds a small `pytest` suite covering:

- governor pacing thresholds (50%, 10% floor, cap at 60s)
- `X-Ratelimit-Limit` parsing (`150/15m`, `1000/1h`, and malformed values)
- park and release on `Retry-After`, including the 60s interactive-wait ceiling
- error-limit park on low `X-ESI-Error-Limit-Remain`
- optimistic no-delay behaviour before any header has been seen
- cache expiry, ETag round-trip, 304 handling
- background declines while interactive proceeds

Tests run against fake response objects: pure logic, no network, no Qt. This
adds a dev dependency and a CI step.

## Non-goals

- No rewrite of third-party clients (zkill, wanderer, evescout).
- No change to the OAuth/PKCE flow.
- No `requests-cache` or other new runtime dependency.
- No prefetching or speculative warming of the cache.

## References

- [Rate Limiting - EVE Developer Docs](https://developers.eveonline.com/docs/services/esi/rate-limiting/)
- [Best Practices for ESI](https://developers.eveonline.com/docs/services/esi/best-practices/)
- [Hold your horses: introducing rate limiting to ESI](https://developers.eveonline.com/blog/hold-your-horses-introducing-rate-limiting-to-esi)
