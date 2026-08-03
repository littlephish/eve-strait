"""Thin ESI client: assets -> dockable structures/stations the character uses."""
from __future__ import annotations

from dataclasses import dataclass

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
    def structure(self, structure_id: int) -> dict | None:
        try:
            return self._get(f"/universe/structures/{structure_id}/").json()
        except requests.HTTPError:
            return None  # no docking access / not resolvable

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
                        type_id=data.get("type_id", 0)))
        results.sort(key=lambda d: d.name)
        return results
