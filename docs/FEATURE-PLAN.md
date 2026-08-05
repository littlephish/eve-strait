# Eve-Strait feature plan

Working plan for the next round of features. Each entry records the **data source**,
whether that source actually exists (verified, not assumed), the **effort**, and what it
depends on.

Verification done 2026-08-05 against live ESI: the spec exposes **180 endpoints**.

---

## 1. AI chat

**Status:** ready to build. No new data source.

The router already computes exact answers. The model's job is to turn a sentence into the
right settings and to explain results, never to calculate. Every number shown still comes
from `jump/router.py`.

| Piece | Detail |
|---|---|
| Value | The route panel now exposes ship, skills, balance, gate preference, docking policy, Ansiblex, incursions, hostile filter, reactivation strategy and avoid lists. One sentence beats hunting for all of it. |
| Shape | Anthropic SDK tool runner. Tools map 1:1 onto existing methods: `set_ship`, `set_skills`, `add_waypoint`, `set_options`, `plan_route`, `explain_route`, `find_system`. |
| UI | Another dock panel, so it inherits float / reset behaviour. |
| Model | `claude-opus-5` by default, user-selectable. A route plan is a few thousand tokens. |
| Key | User supplies their own, stored beside `client_id`. |

**Explain mode is the underrated half.** `analyze_gate_assist` already computes mandatory
chokepoints, gate spans and per-leg failure reasons, then throws most of it into a table.
Feeding that structured output to the model answers "why does this route gate through
Paala?" with zero hallucination risk, because every fact is supplied rather than recalled.

**Privacy gate.** Dockables, structure names, standings and staging systems are
intelligence. Opt-in, off by default, with an explicit list of what leaves the machine and
an option to send system names only.

---

## 2. Alexa integration

**Status:** feasible, but narrow. Build after 7 and 8.

**Verified:** the core (`data/`, `jump/`, `esi/`, `config.py`) imports zero PySide6 and
loads standalone, so the routing engine runs in Lambda without the GUI. No rewrite needed.

**Two hard constraints, both real:**

1. **Null-sec names break voice.** `SF-XJS`, `1DQ1-A`, `HB-5L3`. Speech recognition
   mangles them going in and text to speech butchers them coming out. Voice routing works
   for Jita, Amarr, Turnur, Dodixie. It does not work where capitals actually operate.
2. **Alexa needs a public endpoint.** Skills invoke Lambda or HTTPS, not the app on your
   desk. Either host the routing core (fine, static SDE plus pure Python) or tunnel to the
   PC (fragile, needs it awake).

**So scope it to timers, not routing.** That is the genuine hands-free need: you are
sitting in a POS shield waiting out a blue timer and alt-tabbing is exactly what you do not
want to do.

- "When can I jump again?" -> reactivation remaining, fatigue clear time
- App schedules an Alexa reminder on jump, so it announces the timer unprompted
- Named-system distance queries as a bonus

Skip voice route planning entirely.

---

## 3 + 5. Kill activity and pod kills per system

**Status:** ready to build. Both are one call.

**Verified live:** `GET /universe/system_kills/` returns **2935 systems**, each with
`ship_kills`, `npc_kills` and `pod_kills`. Public, no auth, hourly cache (`expires`
header). Items 3 and 5 are the same endpoint, not two features.

Bonus from the same family: `GET /universe/system_jumps/` returns `ship_jumps` for **4995
systems**, which is gate traffic. Useful for spotting a camped pipe versus a dead one.

**Uses, in value order:**

1. **Map overlay.** Heat by `ship_kills` on the existing system dots. The map already
   batches draw calls, so this is cheap.
2. **Routing input.** A `danger` weight in `plan_multimodal`, same shape as the existing
   `haven` bias: penalise landing in a system with recent kills. This is the piece GTS had
   as "kill activity filtering".
3. **Route panel column.** Kills in the last hour per waypoint.
4. **Pod kills specifically** flag gate camps rather than fleet fights: a system with pod
   kills but few ship kills is a smartbomb camp.

Cache to disk with the `expires` header, same pattern as the SDE.

---

## 4. Cyno counts per system

**Status: not possible as specified.** Needs redesign.

**Verified:** searching all 180 ESI endpoints for `cyno` returns **zero matches**. CCP does
not expose cyno lightings, and never has. There is no count to fetch.

What is achievable is a **proxy**, and it should be labelled as one:

| Approach | What it gives | Honesty |
|---|---|---|
| zKillboard killmails filtered to cyno-fit hulls (Force Recon, Blockade Runner, ships with a Cynosural Field Generator) | Systems where a cyno ship *died* | Undercounts massively. Most cynos never die. |
| `system_kills` + `system_jumps` as a general activity signal | Where capitals are likely operating | Honest, no cyno claim |

**Recommendation:** drop "cyno counts" and ship the activity overlay from item 3 instead.
If we want the zKill proxy, label it "cyno ship losses", never "cyno activity", or the
number is a lie.

**Better use of the same effort: cyno jammers.** A Tenebrex Cyno Jammer (type `37534`,
already in `docking.py`) prevents cyno lighting in its system, which means **you cannot
jump into it at all**. That is a hard routing constraint we do not model today and it
matters far more to a capital pilot than a kill count. GTS had exactly this.

---

## 6. GARPA / GTS features worth stealing

GARPA is the Goonfleet team behind **GTS (Garpa Topographical Survey)**, a capital and
sub-capital route planner. Feature list from CCP's community spotlight and the forum
thread. What we already have is omitted; this is only the gap.

| GTS feature | Fit | Notes |
|---|---|---|
| **Cyno jammer awareness** | High | Hard constraint: blocks jumping into the system. Type ID already known. See item 4. |
| **Kill activity filtering in routing** | High | Falls out of item 3 for free. |
| **Actual in-game travel time** | High | We use a flat 1 minute per gate. Real time is align plus warp plus session change and varies with system size and ship. A better estimate makes the totals trustworthy. |
| **Waypoint optimisation (many waypoints)** | High | Travelling-salesman ordering for a delivery run. We route through waypoints in the order given; GTS reordered them. Genuinely useful for JF logistics. |
| **Saved / favourite routes** | Medium | Jita to staging is a route you run weekly. Cheap to add next to pinned docks. |
| **System notes** | Medium | Per-system free text ("gate camp", "friendly Fortizar"), persisted. Cheap. |
| **Complex galactic queries** | Medium | "Nearest system with a clone bay", "nearest station of corp X". Needs the station/service data we already download. |
| **Cyno alt support** | Medium | Model a cyno character staged in a system, so routing knows where you can actually light. Pairs with item 8. |
| **Hard lowsec avoidance** | Low | We have a gate security preference; a hard "never lowsec" toggle is a small addition. |
| **Jump range sharing** | Low | Export the reach circle as a shareable list. |

---

## 7. Current player location

**Status:** ready to build. **Scope already granted.**

`GET /characters/{id}/location/` returns `solar_system_id` plus station or structure.
`esi-location.read_location.v1` is already in `config.SCOPES` and already requested at
login, so this needs no re-authentication. `GET /characters/{id}/online/` is available too.

- "You are here" marker on the map
- **Use current location as the route origin** by default, which removes the most common
  first step of every plan
- Warn when the planned origin is not where the character actually is

Highest value per unit of work in the whole list.

---

## 8. Multiple characters

**Status:** ready to build. Prerequisite for the per-character waypoint work.

Today `token.json` holds exactly one token. Multi-character means:

| Change | Detail |
|---|---|
| Token store | `tokens.json` keyed by `character_id`, each with its own scopes. Migrate the existing single token in on first run, same pattern as the `eve-jump-planner` to `eve-strait` data move. |
| Character switcher | In the character panel. Active character drives dockables, standings, starbases, location. |
| Dockables cache | Already keyed by `character_id`, so it is correct as-is. |
| **Set waypoint picks a character** | Right-click "Set in-game destination" becomes a submenu listing linked characters. This is the concrete reason multi-character has to land before more ESI write actions. |
| Standings | Merge or keep per character? Recommend per character, since a scout alt in a different corp has different contacts. |

Pairs naturally with **cyno alts** from the GTS list: a linked alt parked in a staging
system is exactly the cyno-alt model GTS had.

---

## Suggested order

Ordering is by dependency and by value per unit of effort, not by the numbers above.

1. **7. Current location** and **8. Multi-character.** Small, already scoped, and 8 gates
   the per-character waypoint behaviour you called out.
2. **3 + 5. Kill and pod activity.** One public endpoint, immediate map and routing value.
3. **Cyno jammers** (the salvageable half of 4) and **kill-weighted routing** (from 3).
   Both are routing-constraint work, so they land together.
4. **1. AI chat.** Self-contained, no hosting, and it is worth more once there is more data
   to ask about.
5. **GTS extras:** travel-time accuracy, waypoint optimisation, saved routes, system notes.
6. **2. Alexa**, scoped to timers only.

## Open questions

- Kill overlay: colour the dots by danger, or a separate toggleable layer? Dots already
  carry security colour.
- Multi-character standings: merged or per character?
- Do we want the zKillboard dependency at all, given item 4 does not deliver what was asked?

## Sources

- [Community Spotlight: GARPA](https://www.eveonline.com/news/view/community-spotlight-garpa)
- [GARPA: Out of game Navigation Tool](https://forums-archive.eveonline.com/topic/226471)
