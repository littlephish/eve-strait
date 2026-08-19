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
`(method, url, sorted params, cache identity)`. Stores body, ETag, `fetched_at`,
`expires_at`, and the response headers.

**Cache identity** is the character ID for private data, so two linked alts
never read each other's assets — and `None` for routes whose response is the
same no matter who asks (`/universe/stations/{id}/`, sovereignty, kills, jumps,
industry, incursions, and `/universe/structures/{id}/`). Sharing those matters
because a bulk load across N characters would otherwise re-resolve identical
station and structure names N times, which is the same tight loop that produced
the dockables 429 in the first place.

`/universe/structures/{id}/` is shared despite being ACL-gated: the response
describes the structure, not the requester, and only successes are ever cached
because a 403 raises before the cache is written. Its only caller looks up
structures from the character's own asset list, so any character reaching the
lookup already had access.

Rate-limit buckets stay keyed per character regardless — those really are
per-character even when the cached body is not.

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
| `/universe/stations/{id}/` | permanent | Immutable. Kills the loop that caused 429 #2. |
| `/universe/structures/{id}/` | 7 days | Name changes only on ownership transfer. |
| assets, contacts, corp structures, starbases | `expires` + ETag | ~1h falls out of the header. |
| `/characters/{id}/location/`, `/ship/`, `/online/` | never cached | Live data by definition. |

`/universe/names/` and `/universe/ids/` are immutable but **not** cached in this
pass: both are POSTs whose input is a JSON body, and the cache key is built from
the URL and query params, not the body. Caching them needs body-aware keying.
They already batch up to 1000 IDs per call, so they are not the burst that
caused either observed 429; this is noted as follow-up work rather than done
badly here.

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

## Addendum: bulk dockables (2026-08-19)

Found while testing this branch. Dockable structures are fetched one character
at a time by an explicit button press. `_switch_character` already reloads the
cached list on every dropdown change and `load_dockables()` reads the files
correctly — but nothing ever fills more than one character's cache per press.
On the development machine, 7 of 12 linked characters had never been fetched,
so switching to them showed nothing, which is indistinguishable from a broken
load.

Decision: add a "Load for all linked characters" context action on the existing
button, walking every linked token and writing each character's cache. The
primary click keeps its current single-character meaning; bulk work should be
chosen, not stumbled into.

Routing semantics do not change. `self.dockables` still holds only the active
character's list, because docking access in EVE is per-character and a
structure in character B's asset list says nothing about whether character A
may dock there. Merging all characters into one routing list would need a
`character_id` on `Dockable` and per-entry attribution in the UI; that is a
larger change and is not attempted here.

This is only affordable because of the shared cache identity described above:
the first character resolves the station and structure names, and the remaining
eleven read them from sqlite.

## Non-goals

- No rewrite of third-party clients (zkill, wanderer, evescout).
- No change to the OAuth/PKCE flow.
- No `requests-cache` or other new runtime dependency.
- No prefetching or speculative warming of the cache.

## Observed limits (measured 2026-08-19)

Taken against live ESI from this machine. Unauthenticated routes only: the
authenticated groups need a linked character and were not measured here.

| Group | Limit | Route probed | Flow |
|---|---|---|---|
| `incursion` | **150/15m** | `/incursions/` | intel refresh |
| `sovereignty` | 600/15m | `/sovereignty/map/` | intel refresh |
| (none yet) | not limited | `/universe/system_jumps/` | intel refresh |

**Token costs confirmed exactly as documented.** Three consecutive 200s each
reported `X-Ratelimit-Used: 2`, and a conditional request answered 304 reported
`X-Ratelimit-Used: 1`. ETag revalidation really does cost half of a fetch, so
the `TOKENS_PER_REQUEST = 2` constant and the ETag design are both sound.

**Did the 150/15m assumption hold?** Partly, and enough to justify the change.
A 150/15m group is real and in production today — `/incursions/` is one. That
means the tight tier this design was built against exists rather than being
inferred from a blog post. It does **not** confirm that `character-location`
specifically sits at 150/15m; that group is authenticated and still unmeasured.

Not every route is limited yet: `/universe/system_jumps/` returned no
`X-Ratelimit-*` headers at all. The governor stays optimistic for those, which
is the correct behaviour — CCP's rollout is still adding groups.

**Outstanding:** run the app with a linked character and debug logging on, and
record the `character-location` and `assets` group limits here. If
`character-location` turns out to be 1000/15m or higher, the 30s polling floor
is conservative rather than necessary and could be revisited on its own merits.

## References

- [Rate Limiting - EVE Developer Docs](https://developers.eveonline.com/docs/services/esi/rate-limiting/)
- [Best Practices for ESI](https://developers.eveonline.com/docs/services/esi/best-practices/)
- [Hold your horses: introducing rate limiting to ESI](https://developers.eveonline.com/blog/hold-your-horses-introducing-rate-limiting-to-esi)
