"""Thin ESI client: assets -> dockable structures/stations the character uses."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import requests

from .. import config
from . import auth


@dataclass
class Dockable:
    location_id: int
    name: str
    solar_system_id: int
    kind: str  # "structure" or "station"
    type_id: int = 0
    max_volume: float = 0.0
    owner_id: int = 0   # owning corp/alliance (structures only)


def incursions() -> set[int]:
    """Set of solar system IDs currently affected by an Incursion. Public."""
    try:
        resp = requests.get(f"{config.ESI_BASE}/incursions/", timeout=20)
        resp.raise_for_status()
        out: set[int] = set()
        for inc in resp.json():
            out.update(inc.get("infested_solar_systems", []))
        return out
    except (requests.RequestException, ValueError):
        return set()


def resolve_names(ids: list[int]) -> dict[int, str]:
    """Resolve entity IDs (corps/alliances/chars/systems) to names. Public."""
    ids = [i for i in ids if i]
    if not ids:
        return {}
    try:
        resp = requests.post(f"{config.ESI_BASE}/universe/names/",
                             json=ids, timeout=20)
        resp.raise_for_status()
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
        self.session = requests.Session()

    # -- auth plumbing ------------------------------------------------------
    def _ensure_token(self):
        if self.token.expired:
            self.token = auth.refresh(self.token, self.client_id)
            auth.save(self.token)

    def _headers(self):
        return {"Authorization": f"Bearer {self.token.access_token}"}

    def _get(self, path: str, **params):
        self._ensure_token()
        url = f"{config.ESI_BASE}{path}"
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=30)
        if resp.status_code == 401:
            self.token = auth.refresh(self.token, self.client_id)
            auth.save(self.token)
            resp = self.session.get(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        return resp

    # -- assets -------------------------------------------------------------
    def assets(self) -> list[dict]:
        cid = self.token.character_id
        out: list[dict] = []
        page = 1
        while True:
            resp = self._get(f"/characters/{cid}/assets/", page=page)
            batch = resp.json()
            out.extend(batch)
            pages = int(resp.headers.get("X-Pages", "1"))
            if page >= pages:
                break
            page += 1
        return out

    # -- name resolution ----------------------------------------------------
    def set_waypoint(self, destination_id: int, add_to_beginning=False,
                     clear_other_waypoints=False) -> None:
        """Set an in-game autopilot waypoint (needs esi-ui.write_waypoint.v1)."""
        self._ensure_token()
        url = f"{config.ESI_BASE}/ui/autopilot/waypoint/"
        params = {
            "destination_id": destination_id,
            "add_to_beginning": str(add_to_beginning).lower(),
            "clear_other_waypoints": str(clear_other_waypoints).lower(),
        }
        resp = self.session.post(url, headers=self._headers(), params=params, timeout=30)
        if resp.status_code == 401:
            self.token = auth.refresh(self.token, self.client_id)
            auth.save(self.token)
            resp = self.session.post(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()

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

    def contacts(self) -> dict[int, float]:
        """Map of contact_id -> standing from your personal contacts."""
        cid = self.token.character_id
        out: dict[int, float] = {}
        page = 1
        while True:
            r = self._get(f"/characters/{cid}/contacts/", page=page)
            for c in r.json():
                out[c["contact_id"]] = float(c.get("standing", 0.0))
            if page >= int(r.headers.get("X-Pages", "1")):
                break
            page += 1
        return out

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
