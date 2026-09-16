# Unified Route Graph and the Cradle of War Ansiblex Changes

**Date:** 2026-09-16
**Status:** Draft design, pending approval
**Supersedes:** the routing sections of `docs/FEATURE-PLAN.md`
**Companion spec:** Hunter Mode (live jump-range confirmation) — to be written separately

## Problem

Two unrelated forces land on the same code.

1. **Cradle of War ships 2026-09-22.** It bans capitals from Ansiblex jump
   bridges, replaces fuel and tolls with a gate capacitor, and scales the cost
   of a jump by ship class and by the destination's distance from the owning
   alliance's capital system. The router does none of this, and as of patch day
   it will confidently plan a carrier through an Ansiblex that carrier cannot
   enter.

2. **The unified-routing proposal** wants wormholes, Thera, Ansiblex, cynos and
   gates in one graph with explained, comparable routes.

Both converge on the same two places: the edge cost function in
`plan_multimodal()` and the per-leg metadata the UI renders. Doing them as two
efforts means touching that cost function twice, so they share one design and,
by decision, one release.

## What already exists

Verified by reading the code on 2026-09-16, not assumed. The proposal was
written from the README and materially understates the engine.

`plan_multimodal()` (`jump/router.py:258-420`) is already a single Dijkstra
relaxing four edge families in one loop — stargate, Ansiblex, wormhole and jump
drive — with security, danger, haven and fuel weighting. The "unified route
graph" is substantially built.

| Proposal item | State | Gap |
|---|---|---|
| Single route graph | Mostly done | Modes are bare strings; no `RouteEdge` type; metadata in a sidecar dict |
| Mapper abstraction | Two providers | EVE-Scout + Wanderer normalise to one shape; informal interface; no Tripwire |
| WH mass/size restrictions | Done, and better than the prior art | Type-code table beats the untrustworthy `max_ship_size` label |
| WH lifetime / mass-status / freshness filters | Missing | Wanderer's `TIME_STATUS`/`MASS_STATUS` are defined but unused |
| Route modes / profiles | Capability yes, presentation no | Weights exist; no named presets |
| Route composition + comparison | Computed, badly surfaced | `analyze_gate_assist()` has savings and chokepoints, buried in a dialog |
| Map visual language | Overlays yes, route no | `draw_route()` styles only gate vs jump; bridge and hole legs look like jumps |
| Ignore a connection | Plumbed, unused | `avoid_edges` reaches the router with zero UI callers |
| Collapse gate runs | Computed, unused | `gate_runs()` exists; nothing displays it |
| Cyno + WH in one route | **Shipped** | Already the differentiator the proposal aspires to |
| Ship-aware routing | **Shipped** | Hull mass vs hole, JDC range, hi-sec cap rules, jammers, Pochven |

The real gap is presentation and filtering, not routing.

## Cradle of War: verified facts

| Change | Detail |
|---|---|
| Capitals and supercapitals banned | Rorqual is the sole exception |
| Capacitor replaces fuel and tolls | 1250 TJ pool, ~200 TJ/hr, ~6.25 h from empty, non-linear; unaffected by remote cap or neuts at release; gate offline at 0 |
| Zone-scaled cost | By destination distance from the owning alliance's capital system: 0-5 ly free, 5.1-10 ×2, 10.1-15 ×6, 15.1-20 ×9, 20.1+ ×15 |
| Ship-class cost | 1.00 TJ base (capsule, hauler, freighter, **jump freighter**) up to 19.00 (Marauder, Rorqual) |
| ACLs alliance-only | Coalition and renter sharing ends |
| Liquid ozone and tolling removed | Structure fuel model gone |
| Cyno jammers 3 → 1 per system | Explicitly to compensate for removing capitals from Ansiblex |

**Jump Freighters and Black Ops keep access.** Both appear in the cost table —
JF at 1.00 TJ (the cheapest tier, alongside a capsule) and Black Ops at 18.00.
The app's flagship use case survives, and a JF is relatively better off than
before: it is the cheapest hull on the network.

### Eligibility by `hull_class`

The ban maps cleanly onto the existing field in `data/ships.py`.

| `hull_class` | Ansiblex | Base TJ |
|---|---|---|
| Subcapital | allowed | varies (not modelled per sub-class) |
| Jump Freighter | allowed | 1.00 |
| Black Ops | allowed | 18.00 |
| Capital Industrial (Rorqual) | allowed, explicit exception | 19.00, zone 2 = 57.00 not 38.00 |
| Carrier | **banned** | — |
| Command Carrier | **banned** | — |
| Dreadnought | **banned** | — |
| Lancer Dreadnought | **banned** | — |
| Force Auxiliary | **banned** | — |
| Supercarrier | **banned** | — |
| Titan | **banned** | — |

Seven of eleven classes lose Ansiblex access.

`Capital Industrial` contains exactly one hull — the Rorqual (`ships.py:70`) —
so the exception is exact and cannot over-permit.

Orcas and standard freighters are **not** in `ships.py`: they have no jump drive,
and this is a jump planner. Neither is a capital, and both keep Ansiblex access
in their own right (Industrial Command Ship 10.00 TJ, Freighter 1.00 TJ). They
fall under the generic `Subcapital` stand-in, which is why per-class TJ cost is
not computable for subcaps — an accepted gap, since subcap capacitor cost is
marginal to a capital and JF planner.

## Verified ESI findings

The alliance capital system **is** reachable, which removes the manual-config
constraint an earlier draft of this design assumed.

```
GET https://esi.evetech.net/sovereignty/systems?compatibility_date=2026-08-18
```

Confirmed live on 2026-09-16: HTTP 200, 1.3 MB, **5,485 systems, 2,712
alliance-claimed, 75 capital systems**. `claim.alliance.is_capital_system`
answers the question directly, for every alliance, unauthenticated.

The payload also carries `sovereignty_hub.vulnerability_window`, `claimed_since`
and split `military_level` / `industrial_level` / `strategic_level`. It is a
strict superset of `/sovereignty/map/`, which the app calls today
(`esi/transport.py:55`), so it should replace that call rather than join it.

### The blocker: disjoint route sets

```
404  /latest/sovereignty/systems               new route absent from legacy
200  /latest/sovereignty/map/                  what we call today
404  /sovereignty/map?compatibility_date=...   old route gone from the new set
404  /sovereignty/systems                      compatibility_date is mandatory
```

`config.py:62` pins `ESI_BASE = "https://esi.evetech.net/latest"` and
`transport.py` builds every URL as `f"{ESI_BASE}{path}"`. Reaching the new data
requires the transport to speak compatibility-dated routes. CCP is sunsetting
`/latest`, so this migration is owed regardless of Ansiblex.

The hull ban needs no new data at all; the zone model drags the transport change
behind it. That asymmetry no longer splits the release, but it does order the
work — see *Work sequence*.

## Decisions

| Decision | Choice |
|---|---|
| Release strategy | **One spec, one release.** Everything lands together after 2026-09-22 |
| Ansiblex eligibility | New `docking.ansiblex_allowed(ship)`, beside `gate_allowed()`; keyed on `hull_class` |
| Where it is enforced | The Ansiblex branch of `plan_multimodal()`, which today checks only the hi-sec gate rule |
| Capital system source | `/sovereignty/systems`, fetched like other sov data; no user configuration |
| Transport migration | Add compatibility-dated base support; migrate sov first, not wholesale |
| Ship-class TJ costs | Read from the SDE on patch day; the dev-blog table is a cross-check, not the source |
| Live gate capacitor | Not modelled. Not exposed, and cannot be known |
| Capacitor in route cost | Cost *per activation* only, shown as a number and a risk, never as a guarantee |
| `RouteEdge` refactor | Last phase, and only if the cost function outgrows its current shape |
| Route freshness | Per-hole age in the route plus a configurable max-age filter |
| Hunter Mode | Out of scope; separate spec |

### Accepted risk of a single release

Between 2026-09-22 and ship date, the released build plans illegal Ansiblex
routes for the seven banned hull classes. This is accepted deliberately in
exchange for one coherent release.

**Interim mitigation, requiring no release:** the existing *Use Ansiblex
network* toggle already disables the network wholesale. Document it as the
workaround for capital pilots — in the README and the GitHub release notes —
as soon as the patch lands.

### Why the capacitor is not simulated

A gate's charge is shared, depleting, and invisible to the API. Simulating it
would produce a confident number that is wrong the moment anyone else jumps.
The honest form is the one the app already uses for wormhole staleness: state
the cost, state the risk, refuse to imply certainty.

"Three zone-5 activations at 15×; a gate this deep can be drained by one fleet"
is useful. A green checkmark is a lie.

## Design

### Edge cost

The hull check is a hard precondition rather than a weight. An ineligible hull
means no edge at all — the same shape as a wormhole too small for the hull,
which already works this way.

**Capacitor cost is reported, not routed on.** An earlier draft proposed
`w_gate × zone_multiplier`. That is wrong: zone 1's multiplier is **zero**, so
it would make every in-zone Ansiblex hop free and let the router chain them
endlessly at no cost. Degenerate routes, not a preference.

Routing cost for an Ansiblex therefore stays `w_gate` — a hop is a hop — and
the TJ figure becomes **leg metadata** the UI displays. Trading hops against
capacitor drain is a preference we have no evidence for yet; inventing a weight
now would bake in a guess. Revisit once real routes exist to argue from.

#### There is only ever one capital system to resolve

An earlier draft assumed gates carry an owner. **They do not.** `get_bridges()`
persists `list[list[str]]` — bare `[nameA, nameB]` pairs (`config.py:273`) — and
`set_bridges()` builds a plain `dict[int, set[int]]` (`universe.py:130`). ESI
discovery filters by owner at adoption time and then discards it.

A first pass concluded this did not matter, reasoning that adoption keeps only
your own corp or alliance's gates so every stored gate must be usable. **That
reasoning is wrong, and multi-character support is why.**

The bridge list is *global config*; alliance membership is *per-character*.
Scan with an alt in alliance X, switch to a main in alliance Y, and the router
plans happily over X's gates. That is a live bug today. Post-patch, with ACLs
alliance-only, it becomes a guaranteed dead end rather than a possible one.

Gates must therefore carry their owner.

#### Bridge records gain an owner

`config` stores bridges as `[[nameA, nameB], ...]`. It becomes a list of
records:

```
{"a": nameA, "b": nameB, "alliance_id": int | None, "source": "esi" | "manual"}
```

**Migration:** existing `[a, b]` pairs load as `alliance_id=None,
source="manual"`.

**Discovered gates already know their owner.** `_apply_ansiblex_pending()`
carries `owner_id` alongside the parsed name pair
(`ui/main_window.py:2733`) and then discards it when it writes
`[a.name, b.name]`. Populating `alliance_id` for ESI-discovered gates costs no
new call — it keeps a value already in hand.

**Hand-pasted gates can be resolved on demand.** Every piece exists:

```
parse_ansiblex_name("A » B")        client.py:40
  → search_structures("A » B")      client.py:382
  → structure(sid)                  client.py:378
  → filter type_id == 35841         client.py:36 (ANSIBLEX_TYPE_ID)
  → owner_id → alliance
```

Two constraints follow. Structure search returns only what that character can
*see*, so a failed lookup is evidence the gate is unusable but not proof —
another reason to warn rather than block. And each resolution costs a search
plus a structure call against the rate-limit governor, so it must be an
explicit **"Resolve owners"** action in the Ansiblex dialog, never automatic on
load.

**Unknown owner is trusted, not blocked.** A legacy or hand-typed entry with no
`alliance_id` remains routable and is flagged in the UI. Blocking it would
silently break every existing user's hand-typed gates on upgrade, and the user
typed them deliberately — they may well be in that alliance. Warn; do not
delete, and do not refuse to route.

**A known, mismatched owner is not routable.** Where `alliance_id` is present
and differs from the active character's alliance, the edge does not exist — the
codebase's idiom for impassable, the same `continue` a wormhole too small for
the hull takes. Not an infinite weight.

There are no standings or coalition checks on Ansiblex legs today, so there is
nothing to bypass; the rule is ownership alone.

**The alliance-only rule applies to Ansiblex Jump Bridges and nothing else.**
CCP confirmed no other navigation or Upwell structure is receiving access
restrictions. This is a hard boundary for the implementation:

- **Do not touch docking.** Dock selection deliberately ranks by standings,
  corp/alliance ownership and configured docking rights (`docking.py`, the
  dock-ranking tiers in the README). Those rules are unchanged, and rentals or
  NAPs that are neutral or red still grant docking. Generalising "ACLs are
  alliance-only now" into dock selection would break it.
- **Do not touch jump portals.** Titan bridges and Black Ops covert bridges are
  unaffected, as are cyno beacons and jammers.
- Stargates, filaments and wormholes are likewise untouched.

The blast radius is one branch of `plan_multimodal()` and the bridge records
that feed it.

This check belongs in the router beside the other edge preconditions, **not** in
`docking.py`. That module answers "what can this hull do" — hull capability.
Gate ownership versus pilot identity is a different question and keeping the
boundary clean matters more than co-locating the two Ansiblex rules.

#### Resolving the capital system

`my_alliance_id` already exists and is populated (`ui/main_window.py:111`):

```
my_alliance_id → /sovereignty/systems → the claim where is_capital_system
```

**Fallback:** when the alliance is unknown (not logged in) or holds no
sovereignty, the multiplier is unavailable. Fall back to flat cost and label the
leg "zone cost unknown" rather than guessing — a wrong multiplier is worse than
an absent one, and silently assuming zone 1 would make every route look free.

#### Zone cost is directional

Cost is set by **the destination's** distance from the capital system. The
distance between the two Ansiblexes is irrelevant. Therefore the same physical
gate pair costs *different amounts in each direction*:

```
A → B   costs zone(distance(B, capital))
B → A   costs zone(distance(A, capital))
```

A gate running outward from the capital is expensive; the same gate flown home
is cheap, and a gate wholly inside zone 1 is free both ways.

This matters because `universe.bridges` is a symmetric adjacency map and the
router relaxes both directions from it. The cost function must be evaluated on
the **edge being traversed**, not on the pair — the one place this is easy to
get wrong, and it would silently produce routes that prefer the wrong direction
around a loop. Worth a dedicated test with an asymmetric fixture.

### Leg metadata

The per-leg table and the map both need to say *why*. Minimum additions:

- Ansiblex: zone, multiplier, TJ cost for this hull, ACL risk flag
- Wormhole: source (EVE-Scout vs Wanderer), age, life and mass status
- Gate runs: collapsed via the existing `gate_runs()`

This is the smallest step toward the proposal's `RouteEdge` without the
rewrite: carry the metadata, keep the mode strings.

### Hand-typed gates

The README promises hand-typed Ansiblex lines are always kept. With ACLs
alliance-only, a hand-typed coalition or renter gate becomes unusable. Those
entries must be flagged, not silently dropped — the user knows things we do
not, and deleting their data on an assumption is worse than warning them.

## Work sequence

One release, but the phases are ordered so that correctness is done and
reviewable before anything depends on new data.

### Phase 1 — Correctness. No new data, no endpoints, no transport work.

1. `docking.ansiblex_allowed(ship)` and its enforcement in the router
2. Bridge records gain `alliance_id` and `source`, with migration from the
   legacy pair format
3. Alliance-match precondition on the Ansiblex edge, unknown owner trusted
4. Flag unknown-owner and mismatched gates in the Ansiblex dialog
5. Correct the README and the router comment that says capitals may bridge
6. Tests for eligibility, migration and alliance matching — the first router
   tests in a suite that currently covers only transport
7. Document the *Use Ansiblex network* workaround in the README

### Phase 2 — Data and model.

8. Compatibility-dated transport support
9. `/sovereignty/systems`, replacing `/sovereignty/map/`
10. Per-class TJ costs and the zone table, reported as leg metadata
11. Confirm fatigue behaviour against the shipped patch notes

### Phase 3 — Presentation. The proposal's UI debt.

12. Style bridge and hole legs in `draw_route()`
13. Wormhole count in the totals footer; savings inline
14. Collapse gate runs in the leg table
15. Per-hole age plus a max-age filter
16. "Ignore this wormhole" wired to `avoid_edges`
17. EOL and mass-status filters
18. Named route profiles; comparison as a panel

### Phase 4 — Optional.

19. `RouteEdge` refactor, if still warranted after phases 1-3

Ship when phases 1-3 are complete.

## Testing

The suite is deliberately narrow — pure logic, no Qt, no network — and this
work fits that shape. `ansiblex_allowed()` is a pure function over `hull_class`;
the zone multiplier is pure over two systems and a capital. Both are testable
without a map or a login, and both are exactly the kind of rule that is silently
wrong for months otherwise.

Fixtures for `/sovereignty/systems` should be a trimmed real response, not a
hand-written one.

## Open questions

1. ~~**Does Ansiblex use still apply pilot jump fatigue?**~~
   **Resolved 2026-09-16: yes, unchanged. The app is already correct.**
   Fatigue has applied to jump bridges since Phoebe (2014): "traveling using a
   Jump Drive, Jump Portal, Jump **Bridge**, Covert Jump Drive or Covert Jump
   Portal causes fatigue to capsuleers." The EVE University page carrying that
   line was last modified 2026-09-05 and records no 2026 change.
   The Cradle of War blogs replace *fuel and tolling* with the capacitor and are
   silent on fatigue; CCP's "we rejected fatigue" remark was about not adopting
   fatigue as the *new* force-projection lever, not about removing the existing
   one. Silence means unchanged. **No code change required** — confirm against
   the shipped patch notes, but do not plan work for it.
2. ~~**Is "Capital Industrial" exactly the Rorqual** in `ships.py`?~~
   **Resolved 2026-09-16:** yes, exactly one hull. See the eligibility table.
3. ~~**Does the zone boundary use the gate's destination or the gate itself?**~~
   **Resolved 2026-09-16: the destination's distance from the capital system.**
   The distance between the two Ansiblexes does not affect cost at all. See
   *Zone cost is directional* below.
4. **Do we adopt the bonus sov data** (vulnerability windows, split ADM indexes)
   now that one call returns it, or ignore it until something needs it? YAGNI
   says ignore.

## Out of scope

- **Hunter Mode** (live jump-range confirmation from an anchor). Separate spec.
- Tripwire provider.
- Simulating live gate capacitor state.
- A wholesale `/latest` migration beyond the sov endpoint.

## References

- [Force Projection: Ansiblex Capacitor Update](https://www.eveonline.com/news/view/force-projection-ansiblex-capacitor-update)
- [Major Update: Force Projection Revamped](https://www.eveonline.com/news/view/major-update-force-projection-revamped)
- [The Future of Force Projection with FC Okami](https://www.eveonline.com/news/view/the-future-of-force-projection-with-fc-okami)
- [esi-issues #1519 — alliance capital system](https://github.com/esi/esi-issues/issues/1519)
