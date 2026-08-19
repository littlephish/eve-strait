"""Thin ESI client: assets -> dockable structures/stations the character uses."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import requests

from .. import config
from . import auth
from .auth import check_response
from .transport import get_transport


class AssetsChangedDuringFetch(RuntimeError):
    """The asset list refreshed while we were walking its pages.

    Concatenating pages either side of a refresh produces a list that never
    existed: items can appear twice or vanish. Better to say so and let the
    caller retry than to cache a torn read for an hour.
    """


@dataclass
class Dockable:
    location_id: int
    name: str
    solar_system_id: int
    kind: str  # "structure" or "station"
    type_id: int = 0
    max_volume: float = 0.0
    owner_id: int = 0   # owning corp/alliance (structures only)


# Ansiblex Jump Bridge (verified against the SDE invTypes dump).
ANSIBLEX_TYPE_ID = 35841


def parse_ansiblex_name(name: str):
    """Ansiblex gates are auto-named "<origin> » <destination>".

    Returns (origin, destination) raw name strings, or None. The caller
    resolves them against the universe, which tolerates trailing junk.
    """
    for sep in ("»", "&raquo;", ">>", "->"):
        if sep in name:
            a, _, b = name.partition(sep)
            a, b = a.strip(" -"), b.strip(" -")
            if a and b:
                return a, b
    return None


def sovereignty(progress=None) -> dict:
    """Who holds sovereignty in each null-sec system. Public, no auth.

    Returns {"owners": {system_id: (owner_id, kind)}, "names": {id: name}}.
    Owner names are resolved up front (a couple of batched calls) so lookups
    during rendering are instant.
    """
    if progress:
        progress("Loading sovereignty map...")
    try:
        resp = get_transport().get("/sovereignty/map/", timeout=45,
                                   priority="background")
        rows = resp.json()
    except (requests.RequestException, ValueError):
        return {"owners": {}, "names": {}}

    owners: dict[int, tuple[int, str]] = {}
    for row in rows:
        sid = row.get("system_id")
        for key, kind in (("alliance_id", "alliance"),
                          ("corporation_id", "corporation"),
                          ("faction_id", "faction")):
            oid = row.get(key)
            if sid and oid:
                owners[sid] = (oid, kind)
                break

    ids = sorted({oid for oid, _ in owners.values()})
    names: dict[int, str] = {}
    for i in range(0, len(ids), 1000):          # /universe/names/ caps at 1000
        if progress:
            progress(f"Resolving sovereignty holders ({i + 1}/{len(ids)})...")
        names.update(resolve_names(ids[i:i + 1000]))
    return {"owners": owners, "names": names}


def system_activity(progress=None, force: bool = False,
                    priority: str = "background") -> dict:
    """Recent activity per solar system. Public, no auth.

    /universe/system_kills/ carries ship, pod and NPC kills for the last hour;
    /universe/system_jumps/ carries gate traffic. Together they show where
    fighting is happening and whether a pipe is busy or dead.

    Returns {"kills": {system_id: {...}}, "jumps": {system_id: int},
             "expires": str}. Empty dicts on failure: activity data is a nice
    to have and must never block routing.
    """
    out = {"kills": {}, "jumps": {}, "expires": ""}
    if progress:
        progress("Loading recent kill activity...")
    try:
        r = get_transport().get("/universe/system_kills/", timeout=45,
                                priority=priority, force=force)
        out["expires"] = r.headers.get("expires", "")
        for row in r.json():
            sid = row.get("system_id")
            if sid:
                out["kills"][sid] = {
                    "ship": row.get("ship_kills", 0),
                    "pod": row.get("pod_kills", 0),
                    "npc": row.get("npc_kills", 0),
                }
    except (requests.RequestException, ValueError):
        return out
    try:
        r = get_transport().get("/universe/system_jumps/", timeout=45,
                                priority=priority, force=force)
        for row in r.json():
            sid = row.get("system_id")
            if sid:
                out["jumps"][sid] = row.get("ship_jumps", 0)
    except (requests.RequestException, ValueError):
        pass
    return out


# Sovereignty Hub. Its vulnerability_occupancy_level is the Activity Defense
# Multiplier, which players raise by mining, ratting and running industry in
# the system. That makes ADM the best public proxy for "how much is actually
# happening here", which is exactly what a hunter wants.
SOV_HUB_TYPE_ID = 32458


def sovereignty_defense(progress=None, force: bool = False,
                        priority: str = "background") -> dict:
    """Per-system ADM and vulnerability window. Public, no auth.

    There is no per-system player count anywhere in ESI. ADM is the closest
    honest signal: a high ADM means sustained mining / ratting / industry.

    Returns {system_id: {"adm": float, "alliance_id": int,
                         "vuln_start": str, "vuln_end": str}}.
    """
    if progress:
        progress("Loading sovereignty defense levels...")
    out: dict[int, dict] = {}
    try:
        r = get_transport().get("/sovereignty/structures/", timeout=45,
                                priority=priority, force=force)
        rows = r.json()
    except (requests.RequestException, ValueError):
        return out
    for row in rows:
        sid = row.get("solar_system_id")
        adm = row.get("vulnerability_occupancy_level")
        if not sid or adm is None:
            continue
        # Keep the highest ADM in the system (the hub is what matters).
        if sid not in out or adm > out[sid]["adm"]:
            out[sid] = {
                "adm": float(adm),
                "alliance_id": row.get("alliance_id"),
                "vuln_start": row.get("vulnerable_start_time", ""),
                "vuln_end": row.get("vulnerable_end_time", ""),
                "type_id": row.get("structure_type_id"),
            }
    return out


def industry_indices(progress=None, force: bool = False,
                     priority: str = "background") -> dict:
    """Per-system industry cost indices. Public, no auth.

    Cost indices rise with industrial job volume, so they are a second
    activity signal alongside ADM. Returns {system_id: {activity: index}}.
    """
    if progress:
        progress("Loading industry indices...")
    out: dict[int, dict] = {}
    try:
        r = get_transport().get("/industry/systems/", timeout=45,
                                priority=priority, force=force)
        rows = r.json()
    except (requests.RequestException, ValueError):
        return out
    for row in rows:
        sid = row.get("solar_system_id")
        if not sid:
            continue
        out[sid] = {c.get("activity"): c.get("cost_index", 0.0)
                    for c in row.get("cost_indices", [])}
    return out


def incursions() -> set[int]:
    """Set of solar system IDs currently affected by an Incursion. Public."""
    try:
        resp = get_transport().get("/incursions/", timeout=20,
                                   priority="background")
        out: set[int] = set()
        for inc in resp.json():
            out.update(inc.get("infested_solar_systems", []))
        return out
    except (requests.RequestException, ValueError):
        return set()


def resolve_ids(names: list[str]) -> dict:
    """Resolve corporation / alliance names to IDs. Public, no auth.

    Returns {"ids": {name_lower: (id, kind)}, "unknown": [names]}.
    """
    names = [n.strip() for n in names if n and n.strip()]
    if not names:
        return {"ids": {}, "unknown": []}
    out: dict[str, tuple[int, str]] = {}
    try:
        resp = get_transport().post("/universe/ids/", json=names, timeout=30)
        data = resp.json()
    except (requests.RequestException, ValueError):
        return {"ids": {}, "unknown": names}
    for key, kind in (("alliances", "alliance"), ("corporations", "corporation")):
        for row in data.get(key) or ():
            out[row["name"].strip().lower()] = (row["id"], kind)
    unknown = [n for n in names if n.strip().lower() not in out]
    return {"ids": out, "unknown": unknown}


def resolve_names(ids: list[int]) -> dict[int, str]:
    """Resolve entity IDs (corps/alliances/chars/systems) to names. Public."""
    ids = [i for i in ids if i]
    if not ids:
        return {}
    try:
        resp = get_transport().post("/universe/names/", json=ids, timeout=20)
        return {row["id"]: row["name"] for row in resp.json()}
    except (requests.RequestException, KeyError, ValueError):
        return {}


def _cache_path(character_id: int):
    return config.CACHE_DIR / f"dockables_{character_id}.json"


def save_dockables(character_id: int, dockables: list[Dockable]) -> None:
    try:
        _cache_path(character_id).write_text(
            json.dumps([asdict(d) for d in dockables]), encoding="utf-8")
    except OSError:
        pass


def load_dockables(character_id: int) -> list[Dockable]:
    path = _cache_path(character_id)
    if not path.exists():
        return []
    try:
        return [Dockable(**d) for d in json.loads(path.read_text(encoding="utf-8"))]
    except (OSError, json.JSONDecodeError, TypeError):
        return []


class EsiClient:
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

    # -- assets -------------------------------------------------------------
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

    # -- name resolution ----------------------------------------------------
    def location(self, priority: str = "interactive") -> dict:
        """Where this character is right now.

        Uses esi-location.read_location.v1, which is already in the default
        scope set, so no re-authentication is needed.
        Returns {"solar_system_id": int, "station_id"?: int, "structure_id"?: int}.
        """
        cid = self.token.character_id
        return self._get(f"/characters/{cid}/location/", priority=priority).json()

    def ship(self, priority: str = "interactive") -> dict:
        """The ship this character is in right now.

        Needs esi-location.read_ship_type.v1. Returns ship_type_id, ship_item_id and
        ship_name; the item id is what fitted modules point at in the asset
        list, which is how a cyno is found.
        """
        cid = self.token.character_id
        return self._get(f"/characters/{cid}/ship/", priority=priority).json()

    def online(self, priority: str = "interactive") -> dict:
        """Login state for this character (esi-location.read_online.v1)."""
        cid = self.token.character_id
        try:
            return self._get(f"/characters/{cid}/online/", priority=priority).json()
        except requests.HTTPError:
            return {}

    def set_waypoint(self, destination_id: int, add_to_beginning=False,
                     clear_other_waypoints=False) -> None:
        """Set an in-game autopilot waypoint (needs esi-ui.write_waypoint.v1)."""
        self._ensure_token()
        params = {
            "destination_id": destination_id,
            "add_to_beginning": str(add_to_beginning).lower(),
            "clear_other_waypoints": str(clear_other_waypoints).lower(),
        }
        cid = self.token.character_id
        resp = self.transport.post("/ui/autopilot/waypoint/", params=params,
                                   character_id=cid, headers=self._headers())
        if resp.status_code == 401:
            self.token = auth.refresh_stored(self.token, self.client_id)
            resp = self.transport.post("/ui/autopilot/waypoint/", params=params,
                                       character_id=self.token.character_id,
                                       headers=self._headers())
        check_response(resp, self.client_id)

    def structure(self, structure_id: int) -> dict | None:
        try:
            return self._get(f"/universe/structures/{structure_id}/").json()
        except requests.HTTPError:
            return None  # no docking access / not resolvable

    def search_structures(self, text: str) -> list[int]:
        """Structure IDs whose name matches ``text`` (needs esi-search scope)."""
        cid = self.token.character_id
        try:
            r = self._get(f"/characters/{cid}/search/", categories="structure",
                          search=text, strict="false")
            return r.json().get("structure", [])
        except requests.HTTPError:
            return []

    def structures_in_system(self, system_name: str, system_id: int,
                             limit: int = 40, progress=None) -> list[Dockable]:
        """Public player structures located in a system (like the in-game
        search). Structures are named "<system> - <name>", so we search the
        system name and keep those that resolve to this system."""
        out: list[Dockable] = []
        for i, sid in enumerate(self.search_structures(system_name)[:limit]):
            if progress:
                progress(f"Resolving structure {i + 1}...")
            data = self.structure(sid)
            if data and data.get("solar_system_id") == system_id:
                out.append(Dockable(sid, data.get("name", str(sid)), system_id,
                                    "structure", type_id=data.get("type_id", 0),
                                    owner_id=data.get("owner_id", 0)))
        return out

    def _paged_contacts(self, path: str) -> dict[int, float]:
        out: dict[int, float] = {}
        page = 1
        while True:
            r = self._get(path, page=page)
            for c in r.json():
                out[c["contact_id"]] = float(c.get("standing", 0.0))
            if page >= int(r.headers.get("X-Pages", "1")):
                break
            page += 1
        return out

    def character_info(self) -> dict:
        return self._get(f"/characters/{self.token.character_id}/").json()

    def corporation_info(self, corp_id: int) -> dict:
        try:
            return self._get(f"/corporations/{corp_id}/").json()
        except requests.HTTPError:
            return {}

    def ansiblex_links(self, progress=None) -> dict:
        """Discover Ansiblex jump bridges via ESI.

        Two sources, merged:
          * your corporation's own structures (needs
            esi-corporations.read_structures.v1 and the Station Manager role);
          * any Ansiblex you have access to that ESI search turns up.

        The gate's *name* carries both endpoints ("A » B"), so one structure
        lookup yields a whole link.
        """
        links: list[list[str]] = []
        errors: list[str] = []
        seen: set[int] = set()

        def add(structure_id: int, name: str):
            parsed = parse_ansiblex_name(name or "")
            if parsed:
                links.append([parsed[0], parsed[1]])

        try:
            info = self.character_info()
            corp_id = info.get("corporation_id")
        except requests.HTTPError as exc:
            corp_id, _ = None, errors.append(f"character info: {exc}")

        if corp_id:
            try:
                page = 1
                while True:
                    if progress:
                        progress(f"Reading corp structures (page {page})...")
                    r = self._get(f"/corporations/{corp_id}/structures/", page=page)
                    for row in r.json():
                        if row.get("type_id") != ANSIBLEX_TYPE_ID:
                            continue
                        sid = row.get("structure_id")
                        if sid in seen:
                            continue
                        seen.add(sid)
                        name = row.get("name") or ""
                        if not name:
                            data = self.structure(sid) or {}
                            name = data.get("name", "")
                        add(sid, name)
                    if page >= int(r.headers.get("X-Pages", "1")):
                        break
                    page += 1
            except requests.HTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                if status == 403:
                    errors.append(
                        "Corp structure list denied (403): your character needs "
                        "the in-game Station Manager role, and the app needs the "
                        "esi-corporations.read_structures.v1 scope. Use "
                        "'Find gates in system' below instead.")
                else:
                    errors.append(f"corp structures: {exc}")

        return {"links": links, "errors": errors}

    def ansiblex_in_system(self, system_name: str, system_id: int,
                           limit: int = 40) -> list[list[str]]:
        """Ansiblex gates in one system, found via structure search."""
        out: list[list[str]] = []
        for sid in self.search_structures(system_name)[:limit]:
            data = self.structure(sid)
            if not data or data.get("type_id") != ANSIBLEX_TYPE_ID:
                continue
            if data.get("solar_system_id") != system_id:
                continue
            parsed = parse_ansiblex_name(data.get("name", ""))
            if parsed:
                out.append([parsed[0], parsed[1]])
        return out

    def starbases(self, progress=None) -> dict:
        """Your corporation's POS control towers, by solar system.

        Needs esi-corporations.read_starbases.v1 and the in-game Director
        role. A POS shield is a real staging option for a capital that cannot
        dock anywhere in the system.
        """
        out: dict[int, int] = {}
        errors: list[str] = []
        try:
            corp_id = self.character_info().get("corporation_id")
        except requests.HTTPError as exc:
            return {"systems": {}, "errors": [f"character info: {exc}"]}
        if not corp_id:
            return {"systems": {}, "errors": ["no corporation"]}
        try:
            page = 1
            while True:
                if progress:
                    progress(f"Reading corp starbases (page {page})...")
                r = self._get(f"/corporations/{corp_id}/starbases/", page=page)
                for row in r.json():
                    sid = row.get("system_id")
                    if sid:
                        out[sid] = out.get(sid, 0) + 1
                if page >= int(r.headers.get("X-Pages", "1")):
                    break
                page += 1
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 403:
                errors.append(
                    "Starbase list denied (403): needs the in-game Director "
                    "role plus esi-corporations.read_starbases.v1.")
            else:
                errors.append(f"starbases: {exc}")
        return {"systems": out, "errors": errors}

    def contacts(self, progress=None) -> dict:
        """Standings from character + corp + alliance contact lists.

        Returns {"standings": {id: standing}, "errors": [str]}. Structure owners
        are corporations, so corp/alliance lists matter as much as personal
        ones; more specific lists win (character > corp > alliance).
        """
        standings: dict[int, float] = {}
        errors: list[str] = []
        char_id = self.token.character_id

        try:
            info = self.character_info()
        except requests.HTTPError as exc:
            info, _ = {}, errors.append(f"character info: {exc}")
        corp_id = info.get("corporation_id")
        alliance_id = info.get("alliance_id")

        # Least specific first so more specific lists overwrite.
        if alliance_id:
            try:
                standings.update(self._paged_contacts(f"/alliances/{alliance_id}/contacts/"))
            except requests.HTTPError as exc:
                errors.append(f"alliance contacts: {exc}")
        if corp_id:
            try:
                standings.update(self._paged_contacts(f"/corporations/{corp_id}/contacts/"))
            except requests.HTTPError as exc:
                errors.append(f"corp contacts: {exc}")
        try:
            standings.update(self._paged_contacts(f"/characters/{char_id}/contacts/"))
        except requests.HTTPError as exc:
            errors.append(f"character contacts: {exc}")

        return {"standings": standings, "errors": errors,
                "corp_id": corp_id, "alliance_id": alliance_id}

    def owner_details(self, owner_id: int) -> dict:
        """Resolve a structure owner (a corporation) to its name and alliance."""
        info = self.corporation_info(owner_id)
        alliance_id = info.get("alliance_id")
        name = info.get("name") or ""
        alliance_name = ""
        if not name or alliance_id:
            names = resolve_names([i for i in (owner_id, alliance_id) if i])
            name = name or names.get(owner_id, str(owner_id))
            if alliance_id:
                alliance_name = names.get(alliance_id, "")
        return {"owner_id": owner_id, "name": name,
                "alliance_id": alliance_id, "alliance_name": alliance_name}

    def station(self, station_id: int) -> dict | None:
        try:
            return self._get(f"/universe/stations/{station_id}/").json()
        except requests.HTTPError:
            return None

    def dockable_locations(self, progress=None) -> list[Dockable]:
        """Distinct stations/structures the character stores assets in.

        These are the places the pilot can dock -> useful jump staging points.
        Station IDs are 60000000-64000000; Upwell structures are > 1e12.
        """
        asset_rows = self.assets()
        location_ids: set[int] = set()
        for row in asset_rows:
            loc = row.get("location_id", 0)
            ltype = row.get("location_type")
            if ltype == "station" or (60_000_000 <= loc < 64_000_000):
                location_ids.add(loc)
            elif loc > 1_000_000_000_000:  # Upwell structure
                location_ids.add(loc)

        results: list[Dockable] = []
        for i, loc in enumerate(sorted(location_ids)):
            if progress:
                progress(f"Resolving location {i + 1}/{len(location_ids)}...")
            if 60_000_000 <= loc < 64_000_000:
                data = self.station(loc)
                if data:
                    results.append(Dockable(
                        loc, data.get("name", str(loc)), data.get("system_id", 0),
                        "station", type_id=data.get("type_id", 0),
                        max_volume=float(data.get("max_dockable_ship_volume", 0.0) or 0.0)))
            else:
                data = self.structure(loc)
                if data:
                    results.append(Dockable(
                        loc, data.get("name", str(loc)),
                        data.get("solar_system_id", 0), "structure",
                        type_id=data.get("type_id", 0),
                        owner_id=data.get("owner_id", 0)))
        results.sort(key=lambda d: d.name)
        return results


def scan_cyno_alts(tokens, client_id: str, cyno_modules: dict[int, str],
                   progress=None, force: bool = False) -> tuple[list, list[str]]:
    """Check every linked character for a fitted cyno.

    Returns (alts, notes). Notes carry the per-character reasons a scan came
    back empty -- a missing scope, a 403, an expired token -- because "no cyno
    alts found" and "could not look" mean very different things to someone
    deciding whether they have a way home.
    """
    from ..data import cyno

    alts, notes = [], []
    for cid, token in sorted(tokens.items()):
        name = getattr(token, "character_name", str(cid))
        if progress:
            progress(f"Checking {name}…")
        try:
            c = EsiClient(token, client_id)
            location = c.location()
            ship = c.ship()
            assets = c.assets(force=force)
        except AssetsChangedDuringFetch:
            notes.append(f"{name}: assets changed mid-read; scan again to "
                         "include this character.")
            continue
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 403:
                notes.append(f"{name}: denied (403) - this character was "
                             "linked before the ship or asset scope was "
                             "granted. Sign in again to include it.")
            else:
                notes.append(f"{name}: {exc}")
            continue
        except Exception as exc:                        # noqa: BLE001
            notes.append(f"{name}: {exc}")
            continue
        alt = cyno.alt_from(cid, name, location, ship, assets, cyno_modules)
        if alt is not None:
            alts.append(alt)
    return alts, notes


def load_all_dockables(tokens, client_id: str, progress=None):
    """Fetch dockable locations for every linked character, in one pass.

    One character at a time by design: asset routes bucket per character, so
    this spreads across N rate-limit buckets rather than hammering one, and
    the station/structure names resolved for the first character are reused
    from cache by the rest.

    Returns ({character_id: [Dockable]}, notes). Notes carry the per-character
    reasons a fetch came back empty, because with a dozen characters "this one
    owns nothing" and "this one could not be read" look identical in a results
    list.
    """
    results: dict[int, list] = {}
    notes: list[str] = []
    for i, (cid, tok) in enumerate(sorted(tokens.items()), start=1):
        name = getattr(tok, "character_name", str(cid))
        if progress:
            progress(f"Loading {name} ({i}/{len(tokens)})…")
        try:
            found = EsiClient(tok, client_id).dockable_locations()
        except AssetsChangedDuringFetch:
            notes.append(f"{name}: assets changed mid-read; try again.")
            continue
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 403:
                notes.append(f"{name}: denied (403) - this character was "
                             "linked before the asset scope was granted. "
                             "Sign in again to include it.")
            else:
                notes.append(f"{name}: {exc}")
            continue
        except Exception as exc:                        # noqa: BLE001
            notes.append(f"{name}: {exc}")
            continue
        results[cid] = found
        save_dockables(cid, found)
    return results, notes
