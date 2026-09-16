# Ansiblex Cradle of War Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Ansiblex routing correct under the Cradle of War rules — capitals barred, gates usable only by their owning alliance — and report each activation's capacitor cost.

**Architecture:** Two hard preconditions are added to the Ansiblex branch of `plan_multimodal()`: a hull-class rule in `docking.py`, and an alliance-ownership match fed by bridge records that now carry an owner. Capacitor cost is computed by a new pure module and attached as leg metadata; it does **not** change routing cost. The alliance capital system needed for zone cost comes from `/sovereignty/systems`, which requires teaching the transport to speak compatibility-dated routes.

**Tech Stack:** Python 3.11-3.13, PySide6 (untouched by this plan except one dialog), stdlib `urllib`/`requests`, pytest. Run everything with `uv run`.

**Spec:** `docs/superpowers/specs/2026-09-16-unified-route-graph-design.md`

## Global Constraints

- Python `>=3.11,<3.14`. Source under `src/eve_strait/`.
- Tests are pure logic — **no Qt, no network, no downloads**. Follow `tests/test_pochven.py`, which builds a synthetic `Universe`.
- Run tests with `uv run pytest`.
- The idiom for an impassable edge is `continue` (no edge at all), never an infinite weight.
- **The alliance-only rule applies to Ansiblex and nothing else.** Do not modify docking rules, dock ranking, standings, docking rights, titan/Black Ops bridges, cyno beacons or jammers.
- Unknown gate owner is **trusted and routable**, and flagged in the UI. Never delete a user's hand-typed gate.
- Live gate capacitor state is never simulated.
- Ansiblex jump fatigue is **unchanged** — do not touch fatigue or reactivation handling.

---

### Task 1: Ansiblex hull eligibility rule

**Files:**
- Modify: `src/eve_strait/data/docking.py` (add after `gate_allowed`, ~line 118)
- Test: `tests/test_ansiblex_rules.py` (create)

**Interfaces:**
- Consumes: `Ship` from `data.ships`
- Produces: `docking.ANSIBLEX_BANNED_HULLS: frozenset[str]`, `docking.ansiblex_allowed(ship: Ship) -> bool`

Note for the implementer: do **not** implement this with the existing `ship_category()` helper. The Rorqual is category `CAPITAL` but is explicitly permitted, so category is the wrong axis. The ban is by `hull_class`, enumerated.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ansiblex_rules.py
"""Who may use an Ansiblex after Cradle of War (2026-09-22).

Capitals and supercapitals lose access entirely. The Rorqual is CCP's sole
industrial exception, and jump freighters and Black Ops were never capitals
for this purpose -- both appear in CCP's own capacitor cost table.
"""
import pytest

from eve_strait.data import docking
from eve_strait.data.ships import SHIPS, SHIPS_BY_NAME


@pytest.mark.parametrize("hull", sorted(docking.ANSIBLEX_BANNED_HULLS))
def test_every_banned_hull_class_exists_and_is_refused(hull):
    """Also guards against a typo in the ban list: a name that matches no
    ship would silently ban nothing."""
    ships = [s for s in SHIPS if s.hull_class == hull]
    assert ships, f"no ship has hull_class {hull!r}"
    for s in ships:
        assert docking.ansiblex_allowed(s) is False


@pytest.mark.parametrize("name", ["Rorqual", "Ark", "Rhea", "Redeemer", "Widow"])
def test_permitted_hulls_keep_access(name):
    assert docking.ansiblex_allowed(SHIPS_BY_NAME[name]) is True


def test_rorqual_is_the_only_capital_industrial():
    """The exception is exact only while this stays true."""
    hulls = [s.name for s in SHIPS if s.hull_class == "Capital Industrial"]
    assert hulls == ["Rorqual"]


def test_generic_subcapital_is_allowed():
    subcaps = [s for s in SHIPS if s.hull_class == "Subcapital"]
    assert subcaps
    for s in subcaps:
        assert docking.ansiblex_allowed(s) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ansiblex_rules.py -v`
Expected: FAIL with `AttributeError: module 'eve_strait.data.docking' has no attribute 'ANSIBLEX_BANNED_HULLS'`

- [ ] **Step 3: Implement**

Add to `src/eve_strait/data/docking.py`, directly after `gate_allowed()`:

```python
# Hull classes barred from Ansiblex jump bridges by the Cradle of War update
# (2026-09-22). Capitals and supercapitals lose access entirely; the Rorqual
# ("Capital Industrial") is CCP's sole industrial exception.
#
# Enumerated by hull_class rather than derived from ship_category(), because
# the Rorqual is a CAPITAL by category and still permitted -- category is the
# wrong axis for this rule.
ANSIBLEX_BANNED_HULLS = frozenset({
    "Carrier",
    "Command Carrier",
    "Dreadnought",
    "Lancer Dreadnought",
    "Force Auxiliary",
    "Supercarrier",
    "Titan",
})


def ansiblex_allowed(ship: Ship) -> bool:
    """May this hull traverse an Ansiblex jump bridge?

    Jump freighters and Black Ops keep access -- both appear in CCP's
    capacitor cost table (JF at 1.00 TJ, Black Ops at 18.00), and neither is
    a capital for this purpose.
    """
    return ship.hull_class not in ANSIBLEX_BANNED_HULLS
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ansiblex_rules.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/eve_strait/data/docking.py tests/test_ansiblex_rules.py
git commit -m "feat(ansiblex): bar capitals from jump bridges per Cradle of War"
```

---

### Task 2: Enforce the hull rule in the router

**Files:**
- Modify: `src/eve_strait/jump/router.py:365-380` (the Ansiblex branch)
- Modify: `README.md` (the "Ansiblex jump gates" section)
- Test: `tests/test_ansiblex_routing.py` (create)

**Interfaces:**
- Consumes: `docking.ansiblex_allowed()` from Task 1
- Produces: no new symbols; `plan_multimodal()` behaviour changes

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ansiblex_routing.py
"""Ansiblex edges obey the Cradle of War hull rule.

Synthetic universe, following tests/test_pochven.py: two systems 20 ly apart
with no stargate between them and an Ansiblex link. That is further than any
hull can jump, so the bridge is the only way through -- which makes "is there
a route at all" a clean proxy for "was the bridge edge offered".
"""
import pytest

from eve_strait.data.ships import SHIPS_BY_NAME, Skills
from eve_strait.data.universe import System, Universe
from eve_strait.jump import router

REGION = 10_000_001


def make(sid, name, x):
    return System(id=sid, name=name, x=x, y=0.0, z=0.0, security=-0.5,
                  region_id=REGION, constellation_id=1)


@pytest.fixture
def uni():
    systems = {1: make(1, "Aaa", 0.0), 2: make(2, "Bbb", 20.0)}
    u = Universe(systems, gates={})
    u.set_bridges([["Aaa", "Bbb"]])
    return u


def plan(uni, ship_name, jdc=5):
    ship = SHIPS_BY_NAME[ship_name]
    return router.plan_multimodal(uni, ship, Skills(jump_drive_calibration=jdc),
                                  uni.systems[1], uni.systems[2])


def test_jump_freighter_may_use_the_bridge(uni):
    result = plan(uni, "Rhea")
    assert result is not None
    _systems, modes = result
    assert modes == ["bridge"]


def test_rorqual_may_use_the_bridge(uni):
    assert plan(uni, "Rorqual") is not None


@pytest.mark.parametrize("name", ["Thanatos", "Revelation", "Apostle", "Avatar"])
def test_capitals_are_refused_the_bridge(uni, name):
    """20 ly with no stargate: without the bridge there is no route at all."""
    assert plan(uni, name) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ansiblex_routing.py -v`
Expected: `test_capitals_are_refused_the_bridge` FAILS — the router currently offers the bridge to every hull, so a route exists.

- [ ] **Step 3: Implement**

In `src/eve_strait/jump/router.py`, replace the Ansiblex comment and loop header. The existing text reads:

```python
        # Ansiblex jump-gate edges: one activation covers any distance, and
        # capitals can use them. Not blocked by "only jumps" -- an Ansiblex is
        # a jump, not a stargate.
        for bid in (universe.bridges.get(nid, ()) if use_ansiblex else ()):
```

Replace with:

```python
        # Ansiblex jump-gate edges: one activation covers any distance. Not
        # blocked by "only jumps" -- an Ansiblex is a jump, not a stargate.
        #
        # Since Cradle of War (2026-09-22) capitals and supercapitals may not
        # use them at all, the Rorqual excepted. An ineligible hull simply has
        # no edge here, the same shape as a wormhole too small to enter.
        bridges = (universe.bridges.get(nid, ())
                   if use_ansiblex and docking.ansiblex_allowed(ship) else ())
        for bid in bridges:
```

Leave the body of the loop unchanged.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS, including the pre-existing Pochven and transport tests.

- [ ] **Step 5: Update the README**

In the "Ansiblex jump gates" section, replace the sentence beginning "Ansiblex legs cost one activation at any distance" with:

```markdown
Ansiblex legs cost one activation at any distance and burn no ship fuel (the
structure pays from its capacitor), but still apply **jump fatigue and a
reactivation timer**.

Since the **Cradle of War** update (22 September 2026), **capitals and
supercapitals cannot use Ansiblex gates at all** - carriers, command carriers,
dreadnoughts, lancer dreadnoughts, force auxiliaries, supercarriers and titans.
The **Rorqual** is the only capital-class exception, and jump freighters and
Black Ops keep full access. An ineligible hull is simply never routed over a
bridge.

Gate access is also **alliance-only**: a gate may be used only by members of
the alliance that owns it. Coalition and renter sharing ended with the same
update.
```

- [ ] **Step 6: Commit**

```bash
git add src/eve_strait/jump/router.py README.md tests/test_ansiblex_routing.py
git commit -m "feat(router): refuse Ansiblex edges to ineligible hulls"
```

---

### Task 3: Bridge records carry an owner

**Files:**
- Modify: `src/eve_strait/config.py:273-281`
- Test: `tests/test_bridge_records.py` (create)

**Interfaces:**
- Produces: `config.get_bridges() -> list[dict]` where each record is
  `{"a": str, "b": str, "alliance_id": int | None, "source": str}`;
  `config.set_bridges(records: list[dict]) -> None`

This changes a persisted schema. Legacy `[a, b]` pairs must keep working.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bridge_records.py
"""Bridge records gained an owner; legacy pairs must still load.

The stored shape was [[nameA, nameB], ...]. It is now a list of records so
the router can tell whose alliance a gate belongs to -- the bridge list is
global config while alliance membership is per character, so without this a
route can be planned over an alt's alliance's gates.
"""
from eve_strait import config


def test_legacy_pairs_migrate_as_untrusted_manual_entries(monkeypatch):
    monkeypatch.setattr(config, "load_config",
                        lambda: {"bridges": [["Aaa", "Bbb"], ["Ccc", "Ddd"]]})
    rows = config.get_bridges()
    assert rows == [
        {"a": "Aaa", "b": "Bbb", "alliance_id": None, "source": "manual"},
        {"a": "Ccc", "b": "Ddd", "alliance_id": None, "source": "manual"},
    ]


def test_records_round_trip(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {"bridges": [
        {"a": "Aaa", "b": "Bbb", "alliance_id": 99, "source": "esi"}]})
    rows = config.get_bridges()
    assert rows[0]["alliance_id"] == 99
    assert rows[0]["source"] == "esi"


def test_malformed_rows_are_dropped(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {"bridges": [
        ["OnlyOne"], [], {"a": "Aaa"}, None, ["Aaa", "Bbb"]]})
    assert config.get_bridges() == [
        {"a": "Aaa", "b": "Bbb", "alliance_id": None, "source": "manual"}]


def test_missing_key_is_empty(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {})
    assert config.get_bridges() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bridge_records.py -v`
Expected: FAIL — `get_bridges()` returns the raw list unchanged.

- [ ] **Step 3: Implement**

Replace `get_bridges()` and `set_bridges()` in `src/eve_strait/config.py`:

```python
def get_bridges() -> list[dict]:
    """Ansiblex jump-gate links as records.

    Each record is {"a", "b", "alliance_id", "source"}. ``alliance_id`` is the
    alliance that owns the gate, or None where it is not known.

    Configs written before the Cradle of War work stored bare [nameA, nameB]
    pairs. Those migrate in as manual entries with no known owner, and an
    unknown owner stays routable: the user typed them deliberately, and
    refusing to route would break their setup on upgrade.
    """
    out: list[dict] = []
    for row in load_config().get("bridges") or ():
        if isinstance(row, dict):
            a, b = row.get("a"), row.get("b")
            source = row.get("source") or "manual"
            alliance_id = row.get("alliance_id")
        elif isinstance(row, (list, tuple)) and len(row) == 2:
            a, b = row[0], row[1]
            source, alliance_id = "manual", None
        else:
            continue
        if not a or not b:
            continue
        out.append({"a": a, "b": b,
                    "alliance_id": alliance_id, "source": source})
    return out


def set_bridges(records: list[dict]) -> None:
    cfg = load_config()
    cfg["bridges"] = records
    save_config(cfg)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_bridge_records.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/eve_strait/config.py tests/test_bridge_records.py
git commit -m "feat(config): bridge records carry owner alliance and source"
```

---

### Task 4: Universe keeps bridge owners

**Files:**
- Modify: `src/eve_strait/data/universe.py:130-147` (`set_bridges`)
- Test: `tests/test_bridge_records.py` (extend)

**Interfaces:**
- Consumes: record shape from Task 3
- Produces: `Universe.set_bridges(records)` accepting **both** records and legacy `[a, b]` pairs, returning the resolved records; `Universe.bridge_owner: dict[tuple[int, int], int | None]` keyed by the sorted id pair

Accepting legacy pairs keeps `tests/test_ansiblex_routing.py` from Task 2 and the MCP server working unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bridge_records.py`:

```python
import pytest

from eve_strait.data.universe import System, Universe

REGION = 10_000_001


def _uni():
    systems = {
        1: System(id=1, name="Aaa", x=0.0, y=0.0, z=0.0, security=-0.5,
                  region_id=REGION, constellation_id=1),
        2: System(id=2, name="Bbb", x=20.0, y=0.0, z=0.0, security=-0.5,
                  region_id=REGION, constellation_id=1),
    }
    return Universe(systems, gates={})


def test_set_bridges_records_owner():
    u = _uni()
    u.set_bridges([{"a": "Aaa", "b": "Bbb", "alliance_id": 99,
                    "source": "esi"}])
    assert u.bridges == {1: {2}, 2: {1}}
    assert u.bridge_owner[(1, 2)] == 99


def test_set_bridges_accepts_legacy_pairs():
    u = _uni()
    u.set_bridges([["Aaa", "Bbb"]])
    assert u.bridges == {1: {2}, 2: {1}}
    assert u.bridge_owner[(1, 2)] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bridge_records.py -v`
Expected: FAIL with `AttributeError: 'Universe' object has no attribute 'bridge_owner'`

- [ ] **Step 3: Implement**

Replace `Universe.set_bridges` in `src/eve_strait/data/universe.py`:

```python
    def set_bridges(self, records) -> list[dict]:
        """Install Ansiblex links from records or legacy [a, b] name pairs.

        Returns the records that resolved, so callers can report bad names.
        ``bridge_owner`` maps the sorted id pair to the owning alliance, or to
        None where the owner is not known -- a hand-typed gate, or one stored
        before owners were recorded.
        """
        bridges: dict[int, set[int]] = {}
        owner: dict[tuple[int, int], int | None] = {}
        resolved: list[dict] = []
        for row in records or ():
            if isinstance(row, dict):
                a_raw, b_raw = row.get("a"), row.get("b")
                alliance_id = row.get("alliance_id")
                source = row.get("source") or "manual"
            elif isinstance(row, (list, tuple)) and len(row) == 2:
                a_raw, b_raw = row
                alliance_id, source = None, "manual"
            else:
                continue
            if not a_raw or not b_raw:
                continue
            a, b = self.match_system(a_raw), self.match_system(b_raw)
            if a is None or b is None or a.id == b.id:
                continue
            bridges.setdefault(a.id, set()).add(b.id)
            bridges.setdefault(b.id, set()).add(a.id)
            owner[(a.id, b.id) if a.id < b.id else (b.id, a.id)] = alliance_id
            resolved.append({"a": a.name, "b": b.name,
                             "alliance_id": alliance_id, "source": source})
        self.bridges = bridges
        self.bridge_owner = owner
        return resolved
```

Also initialise the attribute in `Universe.__init__`, beside `self.bridges`:

```python
        self.bridge_owner: dict[tuple[int, int], int | None] = {}
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS. Task 2's routing test still passes because legacy pairs are accepted.

- [ ] **Step 5: Commit**

```bash
git add src/eve_strait/data/universe.py tests/test_bridge_records.py
git commit -m "feat(universe): track the owning alliance of each Ansiblex link"
```

---

### Task 5: Alliance-match precondition on Ansiblex edges

**Files:**
- Modify: `src/eve_strait/jump/router.py` (signature of `plan_multimodal`, and the Ansiblex branch)
- Test: `tests/test_ansiblex_routing.py` (extend)

**Interfaces:**
- Consumes: `Universe.bridge_owner` from Task 4
- Produces: `plan_multimodal(..., my_alliance_id: int | None = None, ...)`

Default `None` means "alliance unknown" and permits every gate, so all existing callers keep working until Task 6 passes the real value.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ansiblex_routing.py`:

```python
def _owned_uni(alliance_id):
    systems = {1: make(1, "Aaa", 0.0), 2: make(2, "Bbb", 20.0)}
    u = Universe(systems, gates={})
    u.set_bridges([{"a": "Aaa", "b": "Bbb", "alliance_id": alliance_id,
                    "source": "esi"}])
    return u


def _plan(uni, my_alliance_id):
    return router.plan_multimodal(
        uni, SHIPS_BY_NAME["Rhea"], Skills(jump_drive_calibration=5),
        uni.systems[1], uni.systems[2], my_alliance_id=my_alliance_id)


def test_own_alliance_gate_is_usable():
    assert _plan(_owned_uni(99), 99) is not None


def test_other_alliance_gate_is_refused():
    assert _plan(_owned_uni(99), 1234) is None


def test_unknown_owner_is_trusted():
    """Hand-typed and legacy gates must keep working on upgrade."""
    assert _plan(_owned_uni(None), 1234) is not None


def test_unknown_pilot_alliance_permits_everything():
    """Not logged in: we cannot judge, so we do not block."""
    assert _plan(_owned_uni(99), None) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ansiblex_routing.py -v`
Expected: FAIL with `TypeError: plan_multimodal() got an unexpected keyword argument 'my_alliance_id'`

- [ ] **Step 3: Implement**

Add the parameter to `plan_multimodal()`, beside `use_ansiblex`:

```python
    my_alliance_id: int | None = None,
```

Document it in the docstring's rules list:

```
      * an Ansiblex may be used only by the alliance that owns it. An owner we
        do not know is trusted rather than blocked -- a hand-typed gate is the
        user asserting access we cannot verify.
```

Inside the Ansiblex loop, after `b = universe.systems.get(bid)`, add the
ownership test to the existing guard:

```python
        for bid in bridges:
            b = universe.systems.get(bid)
            owner = universe.bridge_owner.get(
                (nid, bid) if nid < bid else (bid, nid))
            if (b is None or blocked(bid) or edge_banned(nid, bid)
                    or not docking.gate_allowed(ship, b.security)
                    or (owner is not None and my_alliance_id is not None
                        and owner != my_alliance_id)):
                continue
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/eve_strait/jump/router.py tests/test_ansiblex_routing.py
git commit -m "feat(router): restrict Ansiblex edges to the owning alliance"
```

---

### Task 6: Preserve the owner at ESI adoption, and pass the pilot's alliance

**Files:**
- Modify: `src/eve_strait/ui/main_window.py:2736-2765` (`_apply_ansiblex_pending`)
- Modify: `src/eve_strait/ui/main_window.py:2299` and `:2841` (the two `plan_multimodal` call sites)
- Modify: `src/eve_strait/ai/mcp_server.py:88` (passes records through unchanged; verify only)

**Interfaces:**
- Consumes: `config.get_bridges()` records (Task 3), `plan_multimodal(my_alliance_id=...)` (Task 5)
- Produces: no new symbols

This task is UI wiring and has no unit test — the suite excludes Qt. Verify by running the app.

- [ ] **Step 1: Keep the owner when adopting discovered gates**

In `_apply_ansiblex_pending()`, the owner is already carried in
`self._ansiblex_pending` and then dropped. Replace the dedupe key, the append
and the save so the record shape is preserved:

```python
        known = {tuple(sorted((r["a"], r["b"]))) for r in config.get_bridges()}
        new_records, still_pending = [], []
        for owner_id, (a_raw, b_raw) in self._ansiblex_pending:
            relation, label = self.owner_relation_cached(owner_id)
            if label not in ("your corporation", "your alliance"):
                if owner_id and owner_id not in self._owners_done:
                    still_pending.append((owner_id, (a_raw, b_raw)))
                continue
            a = self.universe.match_system(a_raw)
            b = self.universe.match_system(b_raw)
            if not a or not b or tuple(sorted((a.name, b.name))) in known:
                continue
            known.add(tuple(sorted((a.name, b.name))))
            new_records.append({"a": a.name, "b": b.name,
                                "alliance_id": self.my_alliance_id,
                                "source": "esi"})
        self._ansiblex_pending = still_pending
        if not new_records:
            return 0
        resolved = self.universe.set_bridges(config.get_bridges() + new_records)
        config.set_bridges(resolved)
```

The gate was adopted only because its owner is your corp or alliance, so
`self.my_alliance_id` is the correct owner to record.

- [ ] **Step 2: Pass the pilot's alliance into both planners**

At both `plan_multimodal(...)` call sites, add:

```python
                   my_alliance_id=self.my_alliance_id,
```

- [ ] **Step 3: Audit the remaining bridge call sites**

Run: `uv run python -c "import eve_strait.ui.main_window, eve_strait.ai.mcp_server"`
Then grep and read each hit, confirming none indexes a bridge row as `row[0]`/`row[1]`:

```bash
grep -rn "get_bridges\|set_bridges" --include=*.py src/
```

Expected remaining sites: `main_window.py:2509`, `:2603-2605`, `:2699`, `:2707-2717`, `mcp_server.py:88`. Update any that treat a row as a two-element sequence.

- [ ] **Step 4: Verify by running the app**

Run: `uv run eve-strait`
Check: existing hand-typed gates still load and route; **File → Ansiblex jump gates… → Load from ESI** still adopts gates; a route over a bridge still plans.

- [ ] **Step 5: Commit**

```bash
git add src/eve_strait/ui/main_window.py src/eve_strait/ai/mcp_server.py
git commit -m "feat(ui): record gate owners on adoption and route by alliance"
```

---

### Task 7: Resolve owners for hand-typed gates on demand

**Files:**
- Modify: `src/eve_strait/ui/dialogs.py` (`AnsiblexDialog`)
- Modify: `src/eve_strait/ui/main_window.py` (handler beside `_load_ansiblex_esi`)

**Interfaces:**
- Consumes: `client.parse_ansiblex_name()`, `client.search_structures()`, `client.structure()`, `client.ANSIBLEX_TYPE_ID`
- Produces: a **Resolve owners** button on `AnsiblexDialog`

On-demand only: each gate costs a search plus a structure call against the rate-limit governor, so this must never run automatically on load.

- [ ] **Step 1: Add the button**

In `AnsiblexDialog`, beside the existing **Load from ESI** button, add:

```python
        self.btn_resolve = QPushButton("Resolve owners")
        self.btn_resolve.setToolTip(
            "Look up who owns each hand-typed gate, so routing can tell "
            "whether your alliance may use it. Costs one ESI search per gate.")
```

- [ ] **Step 2: Implement the handler**

In `main_window.py`, beside `_load_ansiblex_esi`:

```python
    def _resolve_ansiblex_owners(self, dlg):
        """Fill in alliance_id for gates that do not have one.

        Structure search only returns what this character can *see*, so a gate
        we cannot resolve is evidence it is unusable -- not proof. Those keep
        alliance_id None and stay routable, flagged rather than dropped.
        """
        if not self.token:
            dlg.status.setText("Log in with EVE first.")
            return
        if not self.esi:
            self.esi = EsiClient(self.token, config.get_client_id())

        records = config.get_bridges()
        todo = [r for r in records if r.get("alliance_id") is None]
        if not todo:
            dlg.status.setText("Every gate already has a known owner.")
            return

        def work(progress=None):
            found = 0
            for rec in todo:
                name = f"{rec['a']} » {rec['b']}"
                for sid in self.esi.search_structures(name)[:5]:
                    data = self.esi.structure(sid)
                    if not data or data.get("type_id") != client.ANSIBLEX_TYPE_ID:
                        continue
                    owner_id = data.get("owner_id")
                    alliance_id = self._alliance_of_corp(owner_id)
                    if alliance_id:
                        rec["alliance_id"] = alliance_id
                        rec["source"] = "esi"
                        found += 1
                    break
            return records, found

        w = Worker(work)
        w.finished_ok.connect(lambda res: self._on_owners_resolved(dlg, *res))
        self.run_task(w, "Resolving Ansiblex owners")
```

```python
    def _on_owners_resolved(self, dlg, records, found):
        resolved = self.universe.set_bridges(records)
        config.set_bridges(resolved)
        unknown = sum(1 for r in resolved if r.get("alliance_id") is None)
        dlg.status.setText(
            f"Resolved {found} gate(s). {unknown} still unknown - these stay "
            f"usable but cannot be checked against your alliance.")
        self._recalc()
```

Use the existing corp→alliance helper the owner-relation cache already relies
on; if none is exposed, add `_alliance_of_corp(corp_id)` returning
`self._corp_alliance.get(corp_id)`.

- [ ] **Step 3: Flag unknown and mismatched gates in the list**

Where `AnsiblexDialog` renders each row, append a suffix:

```python
            if rec.get("alliance_id") is None:
                suffix = "  [owner unknown]"
            elif (self.my_alliance_id
                  and rec["alliance_id"] != self.my_alliance_id):
                suffix = "  [other alliance - not usable]"
            else:
                suffix = ""
```

- [ ] **Step 4: Verify by running the app**

Run: `uv run eve-strait`
Check: **File → Ansiblex jump gates…** shows `[owner unknown]` against hand-typed gates; **Resolve owners** fills in owners it can see and reports the rest; nothing is deleted.

- [ ] **Step 5: Commit**

```bash
git add src/eve_strait/ui/dialogs.py src/eve_strait/ui/main_window.py
git commit -m "feat(ui): resolve and flag Ansiblex gate owners on demand"
```

---

### Task 8: Compatibility-dated ESI routes

**Files:**
- Modify: `src/eve_strait/config.py:62`
- Modify: `src/eve_strait/esi/transport.py:105-115, 188`
- Test: `tests/test_compat_routes.py` (create)

**Interfaces:**
- Produces: `config.ESI_COMPAT_BASE`, `config.ESI_COMPATIBILITY_DATE`, `config.COMPAT_PATHS: frozenset[str]`, and `transport.url_for(path) -> tuple[str, dict]` returning the URL and any extra query parameters

The legacy and compatibility-dated route sets are **disjoint**: `/sovereignty/systems` exists only on the dated base, `/sovereignty/map/` only on `/latest`. Route per path rather than migrating wholesale.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_compat_routes.py
"""ESI has two disjoint route sets and we need one endpoint from each.

/sovereignty/systems exists only on the compatibility-dated base and requires
the date; /sovereignty/map/ exists only under /latest. So the transport picks
a base per path.
"""
from eve_strait import config
from eve_strait.esi import transport


def test_legacy_path_uses_the_versioned_base():
    url, params = transport.url_for("/sovereignty/map/")
    assert url == f"{config.ESI_BASE}/sovereignty/map/"
    assert params == {}


def test_compat_path_uses_the_dated_base():
    url, params = transport.url_for("/sovereignty/systems")
    assert url == f"{config.ESI_COMPAT_BASE}/sovereignty/systems"
    assert params == {"compatibility_date": config.ESI_COMPATIBILITY_DATE}


def test_compatibility_date_is_pinned():
    """An unpinned date silently changes the route set under us."""
    assert config.ESI_COMPATIBILITY_DATE == "2026-08-18"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_compat_routes.py -v`
Expected: FAIL — `config` has no `ESI_COMPAT_BASE`.

- [ ] **Step 3: Implement**

In `src/eve_strait/config.py`, beside `ESI_BASE`:

```python
# ESI is migrating off the /latest route set onto compatibility-dated routes,
# and the two are disjoint: /sovereignty/systems exists only on the dated base
# and /sovereignty/map/ only under /latest. Paths listed in COMPAT_PATHS are
# sent to the dated base; everything else keeps the versioned one.
#
# The date is pinned deliberately. Leaving it off returns the legacy route set,
# and floating it would let CCP change our route shapes without a release.
ESI_COMPAT_BASE = "https://esi.evetech.net"
ESI_COMPATIBILITY_DATE = "2026-08-18"
COMPAT_PATHS = frozenset({"/sovereignty/systems"})
```

In `src/eve_strait/esi/transport.py`, add:

```python
def url_for(path: str) -> tuple[str, dict]:
    """Full URL and any extra query parameters for an ESI path."""
    if path in config.COMPAT_PATHS:
        return (f"{config.ESI_COMPAT_BASE}{path}",
                {"compatibility_date": config.ESI_COMPATIBILITY_DATE})
    return f"{config.ESI_BASE}{path}", {}
```

Then replace both `url = f"{config.ESI_BASE}{path}"` sites and the cache-key
site so they use `url_for()` and merge the returned parameters into the request
params. The compatibility date **must** be part of the cache key, or a change of
date would serve stale entries.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_compat_routes.py tests/test_transport.py tests/test_httpcache.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/eve_strait/config.py src/eve_strait/esi/transport.py tests/test_compat_routes.py
git commit -m "feat(esi): route compatibility-dated paths to the new base"
```

---

### Task 9: Alliance capital systems from /sovereignty/systems

**Files:**
- Modify: `src/eve_strait/esi/client.py` (beside the existing sovereignty reader, ~line 145)
- Test: `tests/test_capital_systems.py` (create)
- Test fixture: `tests/data/sovereignty_systems.json` (create)

**Interfaces:**
- Consumes: `transport.url_for()` (Task 8)
- Produces: `client.capital_systems(payload: dict) -> dict[int, int]` mapping `alliance_id -> solar_system_id`

Keep parsing pure and separate from fetching so it is testable without network.

- [ ] **Step 1: Create the fixture**

Trim a real response to a handful of rows. Create `tests/data/sovereignty_systems.json`:

```json
{"solar_systems": [
  {"solar_system_id": 30000001, "claim": {"faction": {"faction_id": 500007}}},
  {"solar_system_id": 30000208, "claim": {"alliance": {"alliance_id": 99003581, "is_capital_system": false}}},
  {"solar_system_id": 30000239, "claim": {"alliance": {"alliance_id": 99003581, "is_capital_system": true}}},
  {"solar_system_id": 30004600, "claim": {"alliance": {"alliance_id": 1900696668, "is_capital_system": true}}},
  {"solar_system_id": 30004601, "claim": {}}
]}
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_capital_systems.py
"""Alliance capital systems, which set Ansiblex capacitor zones.

/sovereignty/systems carries is_capital_system on the alliance claim, so one
public call gives every alliance's capital. Parsing is kept separate from
fetching so it tests without network.
"""
import json
from pathlib import Path

from eve_strait.esi import client

FIXTURE = Path(__file__).parent / "data" / "sovereignty_systems.json"


def test_capital_systems_are_extracted():
    payload = json.loads(FIXTURE.read_text())
    assert client.capital_systems(payload) == {
        99003581: 30000239,
        1900696668: 30004600,
    }


def test_non_capital_and_faction_claims_are_ignored():
    payload = json.loads(FIXTURE.read_text())
    caps = client.capital_systems(payload)
    assert 30000208 not in caps.values()
    assert 500007 not in caps


def test_empty_payload_is_empty():
    assert client.capital_systems({}) == {}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_capital_systems.py -v`
Expected: FAIL — `client` has no attribute `capital_systems`.

- [ ] **Step 4: Implement**

Add to `src/eve_strait/esi/client.py`:

```python
def capital_systems(payload: dict) -> dict[int, int]:
    """Map alliance_id -> its capital solar system.

    Only alliance claims carry is_capital_system; faction and unclaimed rows
    are skipped. One public call covers every alliance, which is why this
    needs no configuration from the user.
    """
    out: dict[int, int] = {}
    for row in (payload or {}).get("solar_systems") or ():
        claim = (row.get("claim") or {}).get("alliance") or {}
        if not claim.get("is_capital_system"):
            continue
        alliance_id = claim.get("alliance_id")
        system_id = row.get("solar_system_id")
        if alliance_id and system_id:
            out[alliance_id] = system_id
    return out
```

Add the fetch beside the existing sovereignty reader, going through the
governed transport so it is cached and rate-limited like everything else:

```python
    def sovereignty_systems(self) -> dict:
        """Raw /sovereignty/systems payload. Public, no auth needed."""
        return self._get("/sovereignty/systems").json()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_capital_systems.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/eve_strait/esi/client.py tests/test_capital_systems.py tests/data/sovereignty_systems.json
git commit -m "feat(esi): read alliance capital systems from /sovereignty/systems"
```

---

### Task 10: Ansiblex capacitor cost

**Files:**
- Create: `src/eve_strait/jump/ansiblex.py`
- Test: `tests/test_ansiblex_capacitor.py` (create)

**Interfaces:**
- Produces: `ansiblex.ZONE_BOUNDS`, `ansiblex.zone_for(distance_ly) -> int`, `ansiblex.BASE_TJ`, `ansiblex.capacitor_cost(hull_class, distance_ly) -> float | None`

`capacitor_cost` returns `None` when the cost is unknowable — an unmodelled hull class, or a missing capital system. **This is reporting only.** It must not be fed into the router's edge cost: zone 1's multiplier is zero, so multiplying hop cost by it would make in-zone chains free and produce degenerate routes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ansiblex_capacitor.py
"""Ansiblex capacitor cost per activation.

Zone is set by the DESTINATION's distance from the owning alliance's capital
system; the distance between the two gates is irrelevant. Zone 1 is free.
"""
import pytest

from eve_strait.jump import ansiblex


@pytest.mark.parametrize("ly,zone", [
    (0.0, 1), (5.0, 1), (5.1, 2), (10.0, 2), (10.1, 3),
    (15.0, 3), (15.1, 4), (20.0, 4), (20.1, 5), (99.0, 5),
])
def test_zone_boundaries(ly, zone):
    assert ansiblex.zone_for(ly) == zone


def test_zone_one_is_free():
    assert ansiblex.capacitor_cost("Jump Freighter", 3.0) == 0.0


def test_jump_freighter_is_the_cheapest_tier():
    assert ansiblex.capacitor_cost("Jump Freighter", 7.0) == 2.0


def test_black_ops_scales_with_zone():
    assert ansiblex.capacitor_cost("Black Ops", 7.0) == 36.0
    assert ansiblex.capacitor_cost("Black Ops", 25.0) == 270.0


def test_rorqual_zone_two_is_a_special_case():
    """CCP's table gives 57, not the 38 the multiplier would produce."""
    assert ansiblex.capacitor_cost("Capital Industrial", 7.0) == 57.0
    assert ansiblex.capacitor_cost("Capital Industrial", 12.0) == 104.5


def test_unmodelled_hull_class_is_unknown():
    """Subcaps collapse to one generic entry, so per-class cost is unknowable."""
    assert ansiblex.capacitor_cost("Subcapital", 7.0) is None


def test_banned_hull_has_no_cost():
    assert ansiblex.capacitor_cost("Titan", 7.0) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ansiblex_capacitor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'eve_strait.jump.ansiblex'`

- [ ] **Step 3: Implement**

Create `src/eve_strait/jump/ansiblex.py`:

```python
"""Ansiblex capacitor economics, from Cradle of War (2026-09-22).

An Ansiblex no longer burns liquid ozone or charges a toll. It holds a
capacitor -- 1250 TJ, recharging non-linearly at roughly 200 TJ/hour, about
6.25 hours from empty -- and every ship through it drains an amount set by the
ship's class and by how far the *destination* sits from the owning alliance's
capital system. At zero the gate stops working until it recovers.

Two deliberate omissions.

**Live capacitor state is not modelled.** A gate's charge is shared, depleting
and exposed nowhere in ESI. A simulated figure would be wrong the moment
anybody else jumps, so this module answers "what does this activation cost" and
never "will it work".

**Cost is reported, not routed on.** Zone 1's multiplier is zero, so folding it
into the router's edge cost would make in-zone hops free and let the router
chain them endlessly. A hop stays a hop; this number is metadata.

Cost is also directional: the same gate is expensive outbound from the capital
and cheap coming home.
"""
from __future__ import annotations

# Upper bound in light years for zones 1-4; anything beyond is zone 5.
ZONE_BOUNDS = (5.0, 10.0, 15.0, 20.0)

# Multiplier applied to the hull's base cost, indexed by zone. Zone 1 is free.
ZONE_MULTIPLIER = {1: 0.0, 2: 2.0, 3: 6.0, 4: 9.0, 5: 15.0}

# Base TJ per activation, by this app's hull_class. Only hulls the app models
# appear here; "Subcapital" is a single generic stand-in covering classes whose
# real costs range from 1.00 (freighter) to 19.00 (Marauder), so it is left out
# rather than answered wrongly.
BASE_TJ = {
    "Jump Freighter": 1.0,
    "Black Ops": 18.0,
    "Capital Industrial": 19.0,       # Rorqual
}

# CCP's table gives the Rorqual 57.00 in zone 2, not the 38.00 the multiplier
# implies. Overrides are (hull_class, zone) -> absolute TJ.
COST_OVERRIDES = {("Capital Industrial", 2): 57.0}


def zone_for(distance_ly: float) -> int:
    """Which capacitor zone a destination that far from the capital sits in."""
    for i, bound in enumerate(ZONE_BOUNDS, start=1):
        if distance_ly <= bound:
            return i
    return 5


def capacitor_cost(hull_class: str, distance_ly: float) -> float | None:
    """TJ drained by one activation, or None where it cannot be known.

    None means unknowable rather than free: a hull class this app does not
    model per-class, or one barred from Ansiblex entirely. Callers must show
    it as unknown, never as zero.
    """
    base = BASE_TJ.get(hull_class)
    if base is None:
        return None
    zone = zone_for(distance_ly)
    override = COST_OVERRIDES.get((hull_class, zone))
    if override is not None:
        return override
    return base * ZONE_MULTIPLIER[zone]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ansiblex_capacitor.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/eve_strait/jump/ansiblex.py tests/test_ansiblex_capacitor.py
git commit -m "feat(ansiblex): capacitor cost per activation by hull and zone"
```

---

## Deferred to the presentation plan

These come from the same spec but belong with the route-presentation work, not
here:

- Showing zone, multiplier and TJ cost on the Ansiblex leg in the route table
- Styling bridge and hole legs distinctly in `draw_route()`
- Wormhole count in the totals footer, gate-run collapsing, hole freshness,
  `avoid_edges` wiring, EOL and mass-status filters, named profiles

## Verification before release

- [ ] Confirm against the shipped 2026-09-22 patch notes that Ansiblex jump
      fatigue is unchanged. The spec concludes it is; if the notes say
      otherwise, `jump/mechanics.py` and the bridge leg in `simulate()` need
      revisiting.
- [ ] Confirm the zone boundary is measured to the destination, not the gate.
- [ ] Re-check `BASE_TJ` against the live SDE rather than the dev blog table.
