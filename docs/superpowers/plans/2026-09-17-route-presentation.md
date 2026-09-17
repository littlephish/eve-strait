# Route Presentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a planned route explain itself — every leg type visually distinct, every wormhole's age and limits on screen, and the Ansiblex capacitor cost finally displayed.

**Architecture:** Logic goes into pure functions in `jump/router.py` and `esi/evescout.py` so it can be tested without Qt; the Qt layers stay thin renderers of those results. No routing behaviour changes except where a filter deliberately removes an edge.

**Tech Stack:** Python 3.11-3.13, PySide6, pytest. Run with `.venv/Scripts/python.exe -m pytest` (`uv run` is broken in this environment — "uv trampoline failed to canonicalize script path").

**Spec:** `docs/superpowers/specs/2026-09-16-unified-route-graph-design.md` (Phase 3, plus the *Leg metadata* section)

## Global Constraints

- Tests are pure logic — **no Qt, no network**. Qt-dependent behaviour is verified by a headless render, not a unit test.
- Never present stale wormhole data as equivalent to a gate route. Unknown is shown as unknown, never as a default.
- `capacitor_cost()` returns `None` for unknowable. **Never render `None` as `0`** — zone 1 genuinely costs nothing and a Titan's cost is simply not modelled.
- Do not change edge selection except in Tasks 6 and 7, which add filters the user asked for.
- Follow the existing idiom: an unusable edge is skipped with `continue`, never given an infinite weight.

---

### Task 1: Distinct leg styling on the map

**Files:**
- Modify: `src/eve_strait/ui/map_view.py` (`draw_route`)

**Interfaces:**
- Produces: `MapView.ROUTE_PENS`, a `{mode: (colour, width, style)}` table

`draw_route()` currently styles only gate vs jump, so an Ansiblex and a wormhole both render as a plain jump line. This is the single cheapest readability win in the plan.

- [ ] **Step 1: Replace the pen selection**

In `src/eve_strait/ui/map_view.py`, replace the body of `draw_route`:

```python
    # One entry per leg mode, so a route says what kind of travel each hop is
    # without clicking anything. Dashed for a wormhole because it is temporary,
    # solid heavy for an Ansiblex because it is infrastructure.
    ROUTE_PENS = {
        "gate":   ("#7fb2ff", 1.2, Qt.PenStyle.DotLine),
        "bridge": ("#b266ff", 2.0, Qt.PenStyle.SolidLine),
        "hole":   ("#58d2a0", 1.8, Qt.PenStyle.DashLine),
        "jump":   ("#e0e0e0", 1.6, Qt.PenStyle.SolidLine),
    }
    # A jump the ship cannot actually make, whatever its mode.
    ROUTE_BAD = ("#ff5555", 1.6, Qt.PenStyle.DashLine)

    def draw_route(self, waypoints: list[System], modes: list[str],
                   in_range: list[bool]):
        for i, (a, b) in enumerate(zip(waypoints, waypoints[1:])):
            pa, pb = self._pos[a.id], self._pos[b.id]
            line = QGraphicsLineItem(pa.x(), pa.y(), pb.x(), pb.y())
            mode = modes[i] if i < len(modes) else "jump"
            # Only a jump can be out of range; a gate, bridge or hole either
            # exists for this hull or was never offered as an edge.
            if mode == "jump" and not in_range[i]:
                colour, width, style = self.ROUTE_BAD
            else:
                colour, width, style = self.ROUTE_PENS.get(
                    mode, self.ROUTE_PENS["jump"])
            pen = QPen(QColor(colour), width)
            pen.setStyle(style)
            pen.setCosmetic(True)
            line.setPen(pen)
            line.setZValue(3)
            self.scene_obj.addItem(line)
            self._overlay.append(line)
```

The Ansiblex purple matches the `refresh_bridges()` network colour (178, 102, 255) so a route leg and the underlying network read as the same thing.

- [ ] **Step 2: Verify headlessly**

Run:

```bash
QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -c "
from PySide6.QtWidgets import QApplication
from eve_strait.data.universe import Universe
from eve_strait.ui.map_view import MapView
app = QApplication([])
u = Universe.load(); mv = MapView(u)
ids = list(u.systems)[:5]
ws = [u.systems[i] for i in ids]
mv.draw_route(ws, ['gate','bridge','hole','jump'], [True]*4)
pens = [i.pen().color().name() for i in mv._overlay]
print(pens)
assert len(set(pens)) == 4, 'each mode must be visually distinct'
print('OK')
"
```

Expected: four different colours, `OK`.

- [ ] **Step 3: Commit**

```bash
git add src/eve_strait/ui/map_view.py
git commit -m "feat(map): draw each route leg type distinctly"
```

---

### Task 2: Route composition in the totals footer

**Files:**
- Create: `tests/test_route_summary.py`
- Modify: `src/eve_strait/jump/router.py` (add `compose`)
- Modify: `src/eve_strait/ui/panels/route_panel.py` (`display_plan` footer)

**Interfaces:**
- Produces: `router.compose(plan) -> str`

`RoutePlan` already counts jumps, gates, bridges and holes; the footer prints the first three and silently omits wormholes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_route_summary.py
"""The footer should say what a route is made of.

RoutePlan already counts every mode; the footer omitted wormholes, so a
route that leaned on a scouted hole looked like a plain gate route.
"""
from eve_strait.jump.router import Leg, RoutePlan, compose


def _leg(mode):
    return Leg(src=None, dst=None, mode=mode, distance_ly=1.0, fuel=0,
               in_range=True, cooldown_min=0.0, fatigue_after_min=0.0,
               wait_before_min=0.0, t_depart_min=0.0)


def _plan(*modes):
    return RoutePlan(legs=[_leg(m) for m in modes])


def test_empty_plan_is_empty():
    assert compose(RoutePlan()) == ""


def test_counts_every_mode_present():
    assert compose(_plan("jump", "jump", "gate")) == "2 jumps, 1 gate"


def test_omits_modes_that_are_absent():
    """A pure gate route should not read '0 jumps, 0 wormholes'."""
    assert compose(_plan("gate", "gate")) == "2 gates"


def test_wormholes_and_ansiblex_are_named():
    out = compose(_plan("jump", "gate", "bridge", "hole"))
    assert out == "1 jump, 1 gate, 1 ansiblex, 1 wormhole"


def test_singular_and_plural():
    assert compose(_plan("hole")) == "1 wormhole"
    assert compose(_plan("hole", "hole")) == "2 wormholes"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_route_summary.py -v`
Expected: FAIL, `ImportError: cannot import name 'compose'`

- [ ] **Step 3: Implement**

Add to `src/eve_strait/jump/router.py`, after the `RoutePlan` class:

```python
# Display order and noun for each leg mode. Ordered by how much a reader
# cares: what you flew, then what carried you.
_MODE_NOUNS = (("jump", "jump"), ("gate", "gate"),
               ("bridge", "ansiblex"), ("hole", "wormhole"))


def compose(plan: "RoutePlan") -> str:
    """What this route is made of, e.g. "12 jumps, 3 gates, 1 wormhole".

    Modes that do not appear are left out rather than printed as zero: a
    plain gate route should not advertise the wormholes it did not use.
    """
    counts = {}
    for leg in plan.legs:
        counts[leg.mode] = counts.get(leg.mode, 0) + 1
    parts = []
    for mode, noun in _MODE_NOUNS:
        n = counts.get(mode, 0)
        if n:
            parts.append(f"{n} {noun}" + ("s" if n != 1 else ""))
    return ", ".join(parts)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_route_summary.py -v`
Expected: PASS

- [ ] **Step 5: Use it in the footer**

In `src/eve_strait/ui/panels/route_panel.py`, inside `display_plan`, replace the two lines that build `bridges` and the opening of `self.totals.setText(...)`:

```python
            hrs = plan.total_time_min / 60.0
            warn = ("" if plan.all_in_range else
                    "   ⚠ some legs invalid (range / hi-sec) - use Auto-route to bridge")
            self.totals.setText(
                f"{router.compose(plan)} · "
                f"{plan.total_fuel:,} isotopes · time ≈ {plan.total_time_min:.0f} min "
                f"({hrs:.1f} h) · peak fatigue {plan.peak_fatigue_min:.0f}m · "
                f"peak reactivation {plan.peak_reactivation_min:.1f}m{warn}")
```

Confirm `router` is imported in that module; if not, add `from ...jump import router`.

- [ ] **Step 6: Run the full suite and commit**

```bash
.venv/Scripts/python.exe -m pytest -q
git add src/eve_strait/jump/router.py src/eve_strait/ui/panels/route_panel.py tests/test_route_summary.py
git commit -m "feat(route): show what a route is composed of, wormholes included"
```

---

### Task 3: Collapse gate runs in the leg table

**Files:**
- Create: `tests/test_leg_collapse.py`
- Modify: `src/eve_strait/jump/router.py` (add `collapse_gate_legs`)
- Modify: `src/eve_strait/ui/panels/route_panel.py` (`display_plan`)

**Interfaces:**
- Consumes: `RoutePlan.legs`
- Produces: `router.collapse_gate_legs(legs) -> list[tuple[Leg, int]]`

A 24-gate run currently fills 24 rows and buries the three jumps that matter. `gate_runs()` already exists but works on `(systems, modes)` and is used only by `analyze_gate_assist`, so this is a sibling that works on legs.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_leg_collapse.py
"""Contiguous gate hops collapse to one row.

A capital route can gate twenty times between two jumps; listing each one
buries the jumps, which are the legs that cost fuel and fatigue.
"""
from eve_strait.jump.router import Leg, collapse_gate_legs


def _leg(mode, name=""):
    return Leg(src=name, dst=name, mode=mode, distance_ly=1.0, fuel=0,
               in_range=True, cooldown_min=0.0, fatigue_after_min=0.0,
               wait_before_min=0.0, t_depart_min=0.0)


def test_empty():
    assert collapse_gate_legs([]) == []


def test_single_gate_is_not_collapsed():
    """One gate is not a run; a "x1" label would be noise."""
    legs = [_leg("gate")]
    assert [n for _leg_, n in collapse_gate_legs(legs)] == [1]


def test_contiguous_gates_collapse():
    legs = [_leg("gate"), _leg("gate"), _leg("gate")]
    out = collapse_gate_legs(legs)
    assert len(out) == 1
    assert out[0][1] == 3


def test_non_gate_legs_break_the_run():
    legs = [_leg("gate"), _leg("gate"), _leg("jump"), _leg("gate")]
    out = collapse_gate_legs(legs)
    assert [(l.mode, n) for l, n in out] == [
        ("gate", 2), ("jump", 1), ("gate", 1)]


def test_collapsed_run_keeps_first_and_last_endpoints():
    """The row has to read 'from the start of the run to its end'."""
    legs = [_leg("gate", "A"), _leg("gate", "B"), _leg("gate", "C")]
    (leg, n), = collapse_gate_legs(legs)
    assert leg.src == "A" and leg.dst == "C" and n == 3


def test_other_modes_never_collapse():
    """Two wormholes in a row are two different holes, not one hop twice."""
    legs = [_leg("hole"), _leg("hole")]
    assert [n for _l, n in collapse_gate_legs(legs)] == [1, 1]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_leg_collapse.py -v`
Expected: FAIL, `ImportError: cannot import name 'collapse_gate_legs'`

- [ ] **Step 3: Implement**

Add to `src/eve_strait/jump/router.py`, beside `gate_runs`:

```python
def collapse_gate_legs(legs) -> list[tuple["Leg", int]]:
    """Group contiguous gate legs, for display only.

    Returns [(leg, count)]. The leg carries the run's first source and last
    destination, so one row reads "A -> D, gate x3". Only gates collapse:
    two wormholes in a row are two distinct holes, each needing its own
    signature, and two jumps each cost their own fuel and fatigue.
    """
    from dataclasses import replace

    out: list[tuple[Leg, int]] = []
    run: list[Leg] = []

    def flush():
        if not run:
            return
        merged = replace(run[0], dst=run[-1].dst,
                         distance_ly=sum(l.distance_ly for l in run),
                         fatigue_after_min=run[-1].fatigue_after_min)
        out.append((merged, len(run)))
        run.clear()

    for leg in legs:
        if leg.mode == "gate":
            run.append(leg)
            continue
        flush()
        out.append((leg, 1))
    flush()
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_leg_collapse.py -v`
Expected: PASS

- [ ] **Step 5: Use it in the table**

In `route_panel.display_plan`, iterate collapsed rows instead of raw legs. Change the opening from `for i, leg in enumerate(plan.legs):` to:

```python
        rows = router.collapse_gate_legs(plan.legs)
        self.table.setRowCount(len(rows))
        for i, (leg, count) in enumerate(rows):
```

and in the `elif leg.mode == "gate":` branch replace the mode cell:

```python
            elif leg.mode == "gate":
                label = "gate" if count == 1 else f"gate ×{count}"
                vals = [label, leg.src.name, leg.dst.name, f"{leg.distance_ly:.1f}",
                        "-", "-", f"{leg.fatigue_after_min:.0f}m", "✓"]
```

- [ ] **Step 6: Run the full suite and commit**

```bash
.venv/Scripts/python.exe -m pytest -q
git add src/eve_strait/jump/router.py src/eve_strait/ui/panels/route_panel.py tests/test_leg_collapse.py
git commit -m "feat(route): collapse contiguous gate hops into one row"
```

---

### Task 4: Carry wormhole freshness onto the edge

**Files:**
- Create: `tests/test_hole_freshness.py`
- Modify: `src/eve_strait/esi/evescout.py` (`graph`, plus a new `edge_age_minutes`)

**Interfaces:**
- Produces: `evescout.edge_age_minutes(info) -> float | None`; edges from `graph()` gain `updated_at`

`connections()` reads `updated_at` from EVE-Scout but `graph()` drops it, so a route leg cannot say how old its hole is. Nothing downstream can show freshness until this carries through.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hole_freshness.py
"""How old is the scan behind this wormhole leg?

A wormhole route is only as good as the last time somebody looked at the
hole. Remaining-life hours say how long it should last; the scan age says
how much to trust that number, and they are different questions.
"""
from datetime import datetime, timedelta, timezone

from eve_strait.esi import evescout


def _iso(minutes_ago):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_unknown_when_absent():
    """Absent must read as unknown, never as fresh."""
    assert evescout.edge_age_minutes({}) is None
    assert evescout.edge_age_minutes({"updated_at": None}) is None


def test_age_in_minutes():
    got = evescout.edge_age_minutes({"updated_at": _iso(30)})
    assert got is not None and 29 <= got <= 31


def test_collapsed_edge_takes_the_oldest_end():
    """A Thera crossing is two holes; it is only as fresh as the staler one."""
    info = {"updated_at": _iso(10), "updated_at_all": [_iso(10), _iso(90)]}
    got = evescout.edge_age_minutes(info)
    assert got is not None and 89 <= got <= 91


def test_graph_carries_updated_at():
    conns = [
        {"hub": "Turnur", "system_id": 30000001, "hub_sig": "AAA-111",
         "far_sig": "BBB-222", "size": "large", "wh_type": "B449",
         "hours": 16, "updated_at": _iso(5)},
    ]
    edges = evescout.graph(conns, turnur_id=30002718)
    (info,) = edges.values()
    assert info.get("updated_at") is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_hole_freshness.py -v`
Expected: FAIL, `AttributeError: module 'eve_strait.esi.evescout' has no attribute 'edge_age_minutes'`

- [ ] **Step 3: Implement**

Add to `src/eve_strait/esi/evescout.py`, beside `describe_age`:

```python
def edge_age_minutes(info) -> float | None:
    """Minutes since anybody last touched this edge's scan, or None.

    A collapsed Thera crossing is two holes, so it reports the age of the
    *staler* end: the route is only as trustworthy as its worst link.

    None means unknown, and callers must render it as unknown. Treating a
    missing timestamp as fresh is how an eight-hour-old hole gets presented
    as a live one.
    """
    stamps = (info or {}).get("updated_at_all") or []
    if not stamps:
        stamp = (info or {}).get("updated_at")
        stamps = [stamp] if stamp else []
    ages = [age_hours(s) for s in stamps]
    ages = [a for a in ages if a is not None]
    if not ages:
        return None
    return max(ages) * 60.0
```

In `graph()`, carry the timestamps through. In the Turnur branch add to the dict:

```python
                "updated_at": c.get("updated_at"),
                "updated_at_all": [c.get("updated_at")],
```

and in the Thera pairing branch add:

```python
                "updated_at": a.get("updated_at"),
                "updated_at_all": [a.get("updated_at"), b.get("updated_at")],
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_hole_freshness.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/eve_strait/esi/evescout.py tests/test_hole_freshness.py
git commit -m "feat(wormholes): carry scan age onto the routable edge"
```

---

### Task 5: Show hole age and Ansiblex cost in the leg table

**Files:**
- Modify: `src/eve_strait/ui/panels/route_panel.py` (`display_plan`)

**Interfaces:**
- Consumes: `evescout.edge_age_minutes` (Task 4), `ansiblex.capacitor_cost`, `ansiblex.zone_for`

This is where `capacitor_cost()` finally gets a consumer — it has been tested but unused since the Ansiblex plan.

- [ ] **Step 1: Show the scan age on a wormhole leg**

In the `if leg.mode == "hole":` branch of `display_plan`, extend the label:

```python
            if leg.mode == "hole":
                info = (self.ctx.universe.hole_between(leg.src.id, leg.dst.id)
                        if getattr(self.ctx, "universe", None) else None)
                via = (info or {}).get("via", "wormhole")
                sig = (info or {}).get("sigs", {}).get(leg.src.id)
                label = f"{via.lower()} {sig}" if sig else via.lower()
                # Age goes in the Fuel column, which a hole never uses: a
                # wormhole costs no isotopes, and how old the scan is matters
                # far more to whether this leg is flyable.
                age = evescout.edge_age_minutes(info or {})
                if age is None:
                    age_txt = "age ?"
                elif age < 90:
                    age_txt = f"{age:.0f}m old"
                else:
                    age_txt = f"{age / 60:.1f}h old"
                vals = [label, leg.src.name, leg.dst.name,
                        f"{leg.distance_ly:.2f}", age_txt, "-",
                        f"{leg.fatigue_after_min:.0f}m", "✓"]
```

Add `from ...esi import evescout` to the module imports if absent.

- [ ] **Step 2: Show zone and capacitor cost on an Ansiblex leg**

In the `elif leg.mode == "bridge":` branch:

```python
            elif leg.mode == "bridge":
                # Capacitor cost is the structure's, not the ship's, so it
                # goes in the Fuel column where a bridge shows "-" today.
                cap_txt = "-"
                cap_sys = getattr(self.ctx, "my_capital_system", None)
                if cap_sys is not None:
                    from ...data.universe import Universe
                    d = Universe.distance_ly(leg.dst, cap_sys)
                    tj = ansiblex.capacitor_cost(ship.hull_class, d)
                    zone = ansiblex.zone_for(d)
                    if tj is None:
                        cap_txt = f"zone {zone}, ? TJ"
                    elif tj == 0:
                        cap_txt = f"zone {zone}, free"
                    else:
                        cap_txt = f"zone {zone}, {tj:g} TJ"
                vals = ["ansiblex", leg.src.name, leg.dst.name,
                        f"{leg.distance_ly:.2f}", cap_txt,
                        f"{leg.cooldown_min:.1f}m",
                        f"{leg.fatigue_after_min:.0f}m", "✓"]
```

Add `from ...jump import ansiblex` to the imports, and take the hull from the
context the way the rest of the panel does — `self.ctx.current_ship()`, as used
in `_docks`. Read it once at the top of `display_plan`:

```python
        ship = self.ctx.current_ship()
```

- [ ] **Step 3: Expose the capital system on the context**

In `src/eve_strait/ui/main_window.py`, add a property beside `capital_system_ids`:

```python
    @property
    def my_capital_system(self):
        """This character's alliance's capital, which sets Ansiblex zones."""
        if not (self.universe and self.my_alliance_id):
            return None
        sid = (self.sov_capitals or {}).get(self.my_alliance_id)
        return self.universe.systems.get(sid) if sid else None
```

Unknown stays `None` and the column stays `-`, per the spec: a wrong multiplier is worse than an absent one.

- [ ] **Step 4: Verify by running the app**

Run: `.venv/Scripts/python.exe -m eve_strait`
Check: plan a route containing an Ansiblex and, if any are scouted, a wormhole. The Fuel column should read `zone N, X TJ` and `NNm old` respectively, and a jump leg should be unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/eve_strait/ui/panels/route_panel.py src/eve_strait/ui/main_window.py
git commit -m "feat(route): show wormhole scan age and Ansiblex capacitor cost per leg"
```

---

### Task 6: Wormhole filters — freshness, lifetime, mass

**Files:**
- Create: `tests/test_hole_filters.py`
- Modify: `src/eve_strait/esi/evescout.py` (add `usable`)
- Modify: `src/eve_strait/ui/main_window.py` (`_install_wormholes`)
- Modify: `src/eve_strait/ui/panels/route_panel.py` (controls)

**Interfaces:**
- Consumes: `edge_age_minutes` (Task 4)
- Produces: `evescout.usable(info, *, max_age_min=None, allow_eol=True, allow_reduced_mass=True) -> bool`

Wanderer already records `TIME_STATUS` and `MASS_STATUS` on its connections; nothing reads them.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hole_filters.py
"""Which scouted holes are worth routing over.

Mass and size are already enforced by fits(); these are the judgement
calls a pilot makes on top: how stale is too stale, and will I trust a
hole that is end of life or mass-critical.
"""
from datetime import datetime, timedelta, timezone

from eve_strait.esi import evescout


def _iso(minutes_ago):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_defaults_allow_everything():
    assert evescout.usable({}) is True


def test_max_age_rejects_stale():
    fresh = {"updated_at": _iso(10), "updated_at_all": [_iso(10)]}
    stale = {"updated_at": _iso(200), "updated_at_all": [_iso(200)]}
    assert evescout.usable(fresh, max_age_min=60) is True
    assert evescout.usable(stale, max_age_min=60) is False


def test_unknown_age_is_rejected_when_a_limit_is_set():
    """Asking for fresh data means unknown does not qualify."""
    assert evescout.usable({}, max_age_min=60) is False
    assert evescout.usable({}, max_age_min=None) is True


def test_end_of_life_filter():
    eol = {"life": "end of life"}
    assert evescout.usable(eol, allow_eol=True) is True
    assert evescout.usable(eol, allow_eol=False) is False
    assert evescout.usable({"life": "fresh"}, allow_eol=False) is True


def test_mass_status_filter():
    crit = {"mass": "critical"}
    reduced = {"mass": "reduced"}
    assert evescout.usable(crit, allow_reduced_mass=False) is False
    assert evescout.usable(reduced, allow_reduced_mass=False) is False
    assert evescout.usable(crit, allow_reduced_mass=True) is True


def test_unknown_life_and_mass_are_allowed():
    """EVE-Scout does not report these; absence must not mean bad."""
    assert evescout.usable({}, allow_eol=False,
                           allow_reduced_mass=False) is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_hole_filters.py -v`
Expected: FAIL, no attribute `usable`

- [ ] **Step 3: Implement**

Add to `src/eve_strait/esi/evescout.py`:

```python
def usable(info, *, max_age_min=None, allow_eol=True,
           allow_reduced_mass=True) -> bool:
    """Does this edge pass the pilot's own standards?

    Size and mass limits are not here -- fits() already refuses a hole the
    hull physically cannot enter. These are the judgement calls on top.

    Absence is treated in opposite directions on purpose. An unknown *age*
    fails a freshness limit, because asking for fresh data is asking to be
    sure. An unknown *life or mass status* passes, because EVE-Scout does
    not report either and rejecting on silence would drop every public hole.
    """
    if max_age_min is not None:
        age = edge_age_minutes(info)
        if age is None or age > max_age_min:
            return False
    if not allow_eol and (info or {}).get("life") == "end of life":
        return False
    if not allow_reduced_mass:
        if (info or {}).get("mass") in ("reduced", "critical"):
            return False
    return True
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_hole_filters.py -v`
Expected: PASS

- [ ] **Step 5: Apply the filter when installing edges**

In `main_window._install_wormholes`, filter the merged edge dict before
`self.universe.set_wormholes(edges)`:

```python
        prefs = self.route.hole_filters()
        edges = {k: v for k, v in edges.items() if evescout.usable(v, **prefs)}
```

- [ ] **Step 6: Add the controls**

In `route_panel.__init__`, directly after the `self.chk_holes` block (which ends
`sec_jumps.add(self.chk_holes)`), add three controls following that same
pattern — `compressible(...)`, signal to `self._emit_changed`, then
`sec_jumps.add(...)`:

```python
        self.spin_hole_age = QSpinBox()
        self.spin_hole_age.setRange(0, 1440)
        self.spin_hole_age.setSingleStep(15)
        self.spin_hole_age.setValue(0)
        self.spin_hole_age.setPrefix("Max scan age ")
        self.spin_hole_age.setSuffix(" min")
        self.spin_hole_age.setSpecialValueText("Max scan age  any")
        self.spin_hole_age.setToolTip(
            "Ignore scouted holes nobody has looked at for this long. "
            "0 accepts any age. A hole with no timestamp counts as unknown "
            "and is dropped whenever a limit is set.")
        compressible(self.spin_hole_age)
        self.spin_hole_age.valueChanged.connect(self._emit_changed)
        sec_jumps.add(self.spin_hole_age)

        self.chk_hole_eol = QCheckBox("Allow end-of-life wormholes")
        self.chk_hole_eol.setChecked(True)
        self.chk_hole_eol.setToolTip(
            "An end-of-life hole may collapse within hours. EVE-Scout does "
            "not report lifetime, so its holes are unaffected by this.")
        compressible(self.chk_hole_eol)
        self.chk_hole_eol.toggled.connect(self._emit_changed)
        sec_jumps.add(self.chk_hole_eol)

        self.chk_hole_mass = QCheckBox("Allow mass-reduced wormholes")
        self.chk_hole_mass.setChecked(True)
        self.chk_hole_mass.setToolTip(
            "A reduced or critical hole may collapse on the next ship "
            "through - possibly yours.")
        compressible(self.chk_hole_mass)
        self.chk_hole_mass.toggled.connect(self._emit_changed)
        sec_jumps.add(self.chk_hole_mass)
```

`QSpinBox` needs adding to the `PySide6.QtWidgets` import list. Then:

```python
    def hole_filters(self) -> dict:
        """Wormhole standards as keyword arguments for evescout.usable."""
        age = self.spin_hole_age.value()
        return {"max_age_min": age or None,
                "allow_eol": self.chk_hole_eol.isChecked(),
                "allow_reduced_mass": self.chk_hole_mass.isChecked()}
```

Emit `changed` from each so a filter change re-installs the edges and re-plans.

- [ ] **Step 7: Run the full suite and commit**

```bash
.venv/Scripts/python.exe -m pytest -q
git add src/eve_strait/esi/evescout.py src/eve_strait/ui/main_window.py src/eve_strait/ui/panels/route_panel.py tests/test_hole_filters.py
git commit -m "feat(wormholes): filter by scan age, lifetime and mass status"
```

---

### Task 7: Ignore one wormhole

**Files:**
- Modify: `src/eve_strait/ui/main_window.py` (context menu, avoid set, planner calls)

**Interfaces:**
- Consumes: `plan_multimodal(avoid_edges=...)`, which has existed with **zero callers** since before this plan

The router argument is already plumbed and tested; this is the UI that finally uses it.

- [ ] **Step 1: Hold the ignored edges**

In `MainWindow.__init__`, beside `self.avoided_ids`:

```python
        # Specific connections the user has rejected, as sorted id pairs.
        # Distinct from avoided_ids: the system is fine, this link is not --
        # a hole they know has collapsed, or one they simply do not trust.
        self.ignored_edges: set[tuple[int, int]] = set()
```

- [ ] **Step 2: Offer it on a wormhole system**

In `_map_context`, after the existing `act_hole` block:

```python
        act_ignore = None
        if holes:
            pairs = [p for p in self.universe.hole_info
                     if sid in p and p not in self.ignored_edges]
            if pairs:
                act_ignore = menu.addAction("Ignore this wormhole")
            elif any(sid in p for p in self.ignored_edges):
                act_ignore = menu.addAction("Stop ignoring this wormhole")
```

and in the dispatch:

```python
        elif act_ignore is not None and chosen == act_ignore:
            self.toggle_ignored_hole(sid)
```

- [ ] **Step 3: Implement the toggle**

```python
    def toggle_ignored_hole(self, system_id: int):
        """Reject, or restore, every scouted hole touching this system."""
        touching = {p for p in self.universe.hole_info if system_id in p}
        if touching & self.ignored_edges:
            self.ignored_edges -= touching
            msg = "Wormhole restored."
        else:
            self.ignored_edges |= touching
            msg = ("Wormhole ignored for routing. Right-click again to "
                   "restore it.")
        self.statusBar().showMessage(msg, 5000)
        self._recalc()
```

- [ ] **Step 4: Pass it to both planners**

At both `plan_multimodal`/`route_through` call sites add:

```python
                   avoid_edges=self.ignored_edges,
```

and thread `avoid_edges` through `route_through` the same way `my_alliance_id` was threaded — its signature already accepts it for `plan_multimodal` but `route_through` does not forward it yet.

- [ ] **Step 5: Verify by running the app**

Run: `.venv/Scripts/python.exe -m eve_strait`
Check: with wormholes enabled and a route using one, right-click that system → *Ignore this wormhole* → the route re-plans without it; right-click again restores it.

- [ ] **Step 6: Commit**

```bash
git add src/eve_strait/ui/main_window.py src/eve_strait/jump/router.py
git commit -m "feat(route): let a specific wormhole be ignored"
```

---

## Deferred

**Named route profiles and the comparison panel** (spec item 18) are not in this
plan. Every other item here has an obvious right answer; that one is a UI design
question about how many presets, what they are called, and whether comparison
replaces the Gate Assist dialog or sits beside it. It deserves its own
brainstorming pass rather than being guessed at inside an implementation plan.

`RouteEdge` (Phase 4) stays deferred, and should only be reconsidered once these
tasks show whether the mode strings are actually getting in the way.

## Verification before release

- [ ] Plan a route with all four leg types and confirm each is distinct on the
      map and correctly labelled in the table.
- [ ] Confirm a wormhole leg never shows an age of "0m" when the timestamp is
      missing — it must read "age ?".
- [ ] Confirm an Ansiblex leg for a hull with no modelled cost reads "? TJ" and
      never "0 TJ".
