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
3. Add scopes: `esi-assets.read_assets.v1`, `esi-universe.read_structures.v1`,
   `esi-location.read_location.v1` (and `publicData`).
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

- **Search** a system or **click the map** to add waypoints. The first waypoint is your origin.
- Reorder / remove waypoints; the blue circle shows reach from the selected waypoint.
- **Auto‑route origin → last** finds a fewest‑jumps (or least‑fuel) path through low/null systems.
- The table shows per‑leg LY, fuel, cooldown and fatigue; the footer shows totals.

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
