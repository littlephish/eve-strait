# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

**Eve-Strait** is a PySide6 desktop tool for planning capital / jump-freighter routes in
EVE Online. It renders a pannable 2D map of New Eden, draws jump range, plans multi-jump
routes with fuel and jump-fatigue timing, and optionally authenticates to ESI (OAuth2 PKCE)
to pull the character's dockable structures, standings, and set in-game waypoints.

- Python `>=3.11,<3.14`, GUI is PySide6, HTTP is `requests`.
- Source lives under `src/eve_strait/` (hatchling wheel package).
- Entry point: `src/eve_strait/__main__.py` (`eve-strait` console script).

## Running

The project is run with **uv**:

```bash
uv run eve-strait
# or
uv run python -m eve_strait
```

- First launch downloads solar-system + station coordinate dumps from Fuzzwork and caches
  them locally; map data, ship/skills, dockables and options are cached between runs.
- ESI login is optional. Client ID and tokens live in `%LOCALAPPDATA%\eve-strait\`
  (`config.json`, `token.json`). The OAuth callback is `http://localhost:8635/callback`.
- The committed test suite (`tests/`, run with `uv run pytest`) covers the ESI
  transport layer only: rate-limit maths and cache expiry. It is deliberately narrow --
  pure logic, no network, no Qt. Everything else is still verified by running the app.
  CI (`.github/workflows/ci.yml`) byte-compiles `src/`, imports the package, and runs
  that suite.

## Building and packaging

Packaging is done with **Nuitka** via the PowerShell build script — do not hand-roll
Nuitka/PyInstaller commands:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_exe.ps1
```

- Default (standalone) output: a **program folder** at `dist/Eve-Strait/` plus
  `dist/Eve-Strait-<ver>-win64.zip`. This is the form to ship.
- `-OneFile` builds a single self-extracting exe for personal use only — AV tools flag it;
  never ship it.
- `-Clean` wipes the persistent build workspace; `-Isolated` builds in a one-off `%TEMP%`
  dir (only needed to run two builds side by side).
- The build workspace is stable at `%LOCALAPPDATA%\eve-strait-build` on purpose: it keeps
  the venv and Nuitka's incremental `.build` dir between runs. Don't move it back to `%TEMP%`.
- Nuitka cannot resolve real paths under OneDrive, so the script builds in a copy elsewhere
  and copies the result back. Don't "fix" this by building in place.
- If Inno Setup is installed, the script also produces `dist/Eve-Strait-<ver>-setup.exe`
  from `dist_assets/win/eve-strait.iss` (per-user install, no UAC).

## Releasing

Releases are tag-driven (`.github/workflows/release.yml`, tags only):

```bash
git tag v0.2.0
git push origin v0.2.0
```

- The workflow builds on `windows-latest` and opens a draft GitHub release.
- It stamps the tag version into `__init__.py` and `pyproject.toml` via
  `scripts/version.py` **before** install and Nuitka. If you touch version handling, keep
  that stamp step working — skipping it is how a release once reported the old version and
  the updater offered an update to the running build.
- The in-app updater (Help → Check for updates) does a folder swap of the release zip;
  `update-log.txt` next to the exe records each step.

## Layout

```
src/eve_strait/
  __main__.py         entry point
  config.py           paths, ESI endpoints, constants
  data/ships.py       jump-capable hull data + skills (editable if CCP rebalances)
  data/docking.py     ship/structure docking rules + NPC station safety
  data/universe.py    SDE loader, light-year geometry, NPC stations
  jump/mechanics.py   range / fuel / fatigue maths
  jump/router.py      route simulation + jump pathfinding
  esi/                ESI auth (PKCE), client, assets, zkill, sovereignty, etc.
  ai/                 in-app agent: MCP server, bridge, providers, tools
  ui/                 map view, main window, panels, dialogs, theme
scripts/
  build_exe.ps1       Nuitka build (see above)
  version.py          read/stamp the package version
  build_mcpb.py       MCP bundle build
  screenshot.py       docs screenshot helper
updater/              Rust updater helper (Cargo project)
dist_assets/win/      Inno Setup script
```

## Domain rules that must stay intact

These are gameplay invariants, not implementation details — don't break them when editing
`jump/` or `data/`:

- You can jump **out of** high-sec but never **into** it: jumps only land in security
  `< 0.5`.
- Capitals cannot use high-sec gates at all; only jump freighters / subcaps can gate the
  final high-sec leg.
- Titan bridge range is 6 ly, Black Ops covert bridge 8 ly.
- Core formulas (see README "How the numbers work"):
  - Max jump range: `base_range_ly × (1 + 0.20 × Jump Drive Calibration)`
  - Fuel/ly: `base_iso × (1 − 0.10·JFC) × (freighter ? 1 − 0.10·JF : 1)`
  - Reactivation timer: `max(1 + ly, preFatigue/10)` minutes, capped at 30
  - New fatigue: `max(10·(1+ly), fatigue·(1+ly))` minutes, capped at 5 h (× implant reduction)
- Docking tiers (Titans/Supercarriers → Keepstar only; carriers/command carriers/dreads
  (incl. lancer)/FAX/Rorqual → NPC stations, Fortizar/Keepstar, XL engineering/refinery;
  jump freighters & subcaps → anything) live in `data/docking.py`.
- Regional stargates (the 116 gates longer than any jump) are taken at **any** gate-balance
  setting — no number of jumps replaces them.

## ESI rate limiting — don't re-derive this, it already exists

There is a real, already-built rate-limit governor. Before touching anything that calls
ESI, read `src/eve_strait/esi/ratelimit.py` and `src/eve_strait/esi/transport.py` in full —
past mistakes in this repo came from assuming a flat poll interval was the current state
and almost re-implementing/bypassing a governor that was already more sophisticated than
that.

- **Every ESI call goes through `get_transport()`** (`esi/transport.py`), never raw
  `requests.get`/`.post`. `EsiTransport` wraps a sqlite `HttpCache` and the
  `RateLimitGovernor` together; that's the one choke point. Verify with
  `grep -n "requests\.\(get\|post\)(" src/eve_strait/esi/client.py` before adding a new
  call site — it should return nothing.
- **Never hardcode a poll interval.** For anything on a timer (location polling, activity
  refresh, etc.), compute the interval from `governor.poll_interval(route, character_id,
  floor=...)`, not a literal like `5000` ms. The `floor` is the only thing a feature should
  choose for itself, and it must be justified against ESI's own per-endpoint cache window
  (e.g. `/characters/{id}/location/` is server-cached 5s — confirmed against ESI's docs,
  which also warn that circumventing that cache can get an application banned. That's the
  floor, not a target to undercut).
- **Interactive requests use `governor.check()`**, background timers use
  `governor.poll_interval()`, 429/`Retry-After` responses go through `governor.park()`, and
  non-2xx/3xx responses feed `governor.observe_errors()` for the legacy error-limit budget.
  Don't add a second, parallel rate-limit mechanism for a new feature — extend the existing
  one if it's missing something.
- **All ESI fetches run off the UI thread**, via the `Worker` (`ui/workers.py`) +
  `MainWindow._run()` pattern, which also registers the job with `TaskRegistry` so it shows
  in the status bar's `BusyIndicator` automatically (`ui/tasks.py`). A new fetch should use
  this path, not a bespoke `QThread` or a synchronous call on the UI thread.
- **`CACHE_POLICY` in `transport.py`** overrides ESI's `Expires` header per-route on
  purpose (e.g. `NEVER` for live-by-definition routes like location/ship/online). If a new
  route needs different caching than ESI's header implies, add it there — don't special-case
  caching logic at the call site.
- There is currently **no user-facing override** of the governor's pacing. If one is added,
  it must not weaken the *reactive* protections (429 handling, error-limit parking) — those
  exist to avoid actually getting banned. An override should only be allowed to relax the
  *proactive* pacing (`check()`'s pacing delay, `poll_interval()`'s throttling), and should
  default to off.

## Conventions

- Windows-first: paths, line endings and the build tooling assume Windows.
- The app must keep working **without** an ESI login; login is an enhancement, never a
  prerequisite for map/routing features.
- ESI scopes are requested as one set at login; if you add a scope, update the README's
  scope list and the `File → Set ESI scopes…` default.
- Ship base ranges/fuel in `data/ships.py` are intentionally hand-editable — keep that file
  simple and data-shaped.
- The in-app AI reaches external models only through the MCP server (`ai/mcp_server.py`);
  there are no direct Claude/OpenAI SDK calls in the app (that dependency tree was removed
  on purpose — don't re-add it).
