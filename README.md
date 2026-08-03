# EVE Jump Planner

A PySide6 desktop tool for planning **capital / jump‑freighter** routes in EVE Online,
in the spirit of the Dotlan jump map. It shows a pannable, zoomable 2D map of New Eden,
draws your jump range, plans multi‑jump routes, and computes **fuel** and **jump‑fatigue
timing** for each leg. It can authenticate to **ESI** and pull the stations / structures
your character has assets in, to use as staging points, and classify where your hull can
actually dock.

## Run

```bash
uv run eve-jump-planner
```

(or `uv run python -m eve_jump_planner`)

The first launch downloads small solar‑system + station coordinate dumps (~a few MB) from
Fuzzwork and caches them locally.

## ESI login (optional)

The map, ranges, fuel and routing all work **without** logging in. Login only adds your
character's dockable structure/station list.

1. Go to <https://developers.eveonline.com> → *Create New Application*.
2. Set the **Callback URL** to **exactly** (note the `/callback` path, no trailing space):
   `http://localhost:8635/callback`
3. Add scopes (or paste the JSON array via *File → Set ESI scopes…*):
   `publicData`, `esi-assets.read_assets.v1`, `esi-universe.read_structures.v1`,
   `esi-location.read_location.v1`, `esi-ui.write_waypoint.v1`,
   `esi-search.search_structures.v1`, `esi-characters.read_contacts.v1`.
4. Copy the **Client ID**. In the app: *File → Set EVE Client ID…* and paste it
   (or set the `EVE_CLIENT_ID` environment variable).
5. *(optional)* *File → Set ESI scopes…* accepts the **JSON scope array** copied
   straight from the dev site, or a space/comma list. It must match your app's
   granted scopes.
6. Click **Log in with EVE**. Uses OAuth2 **PKCE** — no client secret is stored.

Common errors: `invalid_request … redirect URL does not match` means the app's
Callback URL isn't exactly the value above; `invalid_scope` means a requested
scope isn't granted to the app (or the Client ID belongs to a different app).

## How the numbers work

| Quantity | Formula |
|---|---|
| Max jump range | `base_range_ly × (1 + 0.20 × Jump Drive Calibration)` |
| Fuel / ly | `base_iso × (1 − 0.10·JumpFuelConservation) × (freighter ? 1 − 0.10·JumpFreighters : 1)` |
| Reactivation timer | `max(1 + ly, preFatigue/10)` minutes, capped at 30 |
| New jump fatigue | `max(10·(1+ly), fatigue·(1+ly))` minutes, capped at 5 h (× implant reduction) |

Jumps can only land in systems with security **< 0.5**. Titan bridge range (6 ly) and
Black Ops covert bridge range (8 ly) are shown per hull; tick *Plan reach as bridge* to
draw the reach circle at bridge range.

Ship base ranges / fuel live in [`ships.py`](src/eve_jump_planner/data/ships.py) and are
easy to edit if CCP rebalances. Skills default to **JDC 4 / JDO 5 / JFC 4 / JF 4** and are
adjustable in the *Ship & Skills* panel.

## Docking safety

Where a hull can dock is modelled in
[`docking.py`](src/eve_jump_planner/data/docking.py):

- **Titans / Supercarriers** → Keepstar (XL) only.
- **Carriers / Dreads / FAX / Rorqual** → NPC stations, Fortizar/Keepstar, and XL
  engineering/refinery structures.
- **Jump Freighters & subcaps** → anything.
- NPC stations are flagged **safe** (docking ring) or **kickout** (unsafe undock).

The route panel offers: *no docking filter*, *require docking for my hull*, and
*prefer safe docking only* (excludes kickout NPC stations).

## Using it

- **Search** a system (double‑click or right‑click → *Add as waypoint*) or **right‑click the
  map** → *Add as waypoint*. Left‑click only pans / selects. First waypoint = origin.
- **Drag** to reorder waypoints; **right‑click** a waypoint (or a map system) for *Show system
  info*, *Show station info* (render image + owner corp/alliance + standing colour), *Set
  in‑game destination*, *Remove*, *Clear all*.
- Adding a system **out of jump range** auto‑inserts bridging hops (runs in the background
  with a spinner).
- **Auto‑route origin → last** builds a full jump+gate route in the background.
- Each waypoint shows its chosen **dock** (station / player structure / "no dock"); change it
  with the dock picker. **Copy route to clipboard** exports `System - Dock` lines.
- The table shows per‑leg mode, LY, fuel, reactivation and fatigue; the footer shows totals.

### Route options

- **Travel**: *Only jumps* · *Prefer jumping* (jump low/null, gate the forced high‑sec tail) ·
  *Prefer gating* (save fuel/fatigue).
- **Gates**: *Fastest* · *Safer* (prefer high‑sec) · *Less secure* (prefer low/null).
- **Docking filter**: none · require docking · prefer safe (excludes kickout NPC stations).
- **Minimize reactivation timer** — waits out fatigue between jumps so each blue timer stays at
  its floor.
- **Exclude hostile‑owned structures** — drops player structures owned by negative‑standing
  entities (from your contacts).
- **Avoid incursion systems** — routes around systems in an active Incursion.

Ship + skills, the map data, your dockables, and these options are cached between runs.
Public player structures in a route system are pulled via ESI structure search (like the
in‑game search), not just the ones you hold assets in.

## Build a portable EXE (Nuitka)

```bash
powershell -ExecutionPolicy Bypass -File scripts/build_exe.ps1
```

Produces a single-file `dist/eve-jump-planner.exe` (no Python needed to run it).
The first build downloads a C compiler and takes several minutes; the first run
of the EXE downloads the map data into `%LOCALAPPDATA%\eve-jump-planner`.

## Layout

```
src/eve_jump_planner/
  __main__.py         entry point
  config.py           paths, ESI endpoints, constants
  data/ships.py       jump-capable hull data + skills (editable)
  data/docking.py     ship/structure docking rules + NPC station safety
  data/universe.py    SDE loader, light-year geometry, NPC stations
  jump/mechanics.py   range / fuel / fatigue maths
  jump/router.py      route simulation + jump pathfinding
  esi/auth.py         EVE SSO PKCE flow
  esi/client.py       assets → dockable locations
  ui/                 map view + main window
```
