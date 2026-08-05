"""New Eden solar-system geometry loaded from the Fuzzwork SDE dump."""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass

import requests

from .. import config
from ..config import JUMPABLE_SECURITY_MAX, LY_METERS, SDE_CSV_PATH, SDE_CSV_URL

# Region IDs >= 11000000 are wormhole/abyssal/void space (no gates, no static
# geometry we care about for jumps); keep known space only.
_KSPACE_MAX_REGION = 11_000_000

# Spatial grid cell size (light years) for fast range queries.
_GRID_CELL = 5.0


@dataclass(frozen=True)
class System:
    id: int
    name: str
    x: float  # light years
    y: float
    z: float
    security: float
    region_id: int
    constellation_id: int

    @property
    def jumpable(self) -> bool:
        """A jump drive can only land in security < 0.5."""
        return self.security < JUMPABLE_SECURITY_MAX


@dataclass(frozen=True)
class Station:
    id: int
    name: str
    system_id: int
    type_id: int
    type_name: str
    max_volume: float


class Universe:
    def __init__(self, systems: dict[int, System],
                 regions: list[tuple[str, float, float]] | None = None,
                 gates: dict[int, set[int]] | None = None,
                 region_names: dict[int, str] | None = None):
        self.systems = systems
        self._by_name = {s.name.lower(): s for s in systems.values()}
        # (name, x_ly, z_ly) region label anchors.
        self.regions = regions or []
        self.region_names = region_names or {}
        # Stargate adjacency: system_id -> set of gate-connected system_ids.
        self.gates = gates or {}
        # Ansiblex jump-gate adjacency (player-built, user-configured).
        self.bridges: dict[int, set[int]] = {}
        # Populated lazily by load_stations().
        self.stations: dict[int, Station] = {}
        self.system_stations: dict[int, list[Station]] = {}
        self.station_type_names: dict[int, str] = {}
        self._grid: dict[tuple[int, int, int], list[System]] = {}
        self._build_grid()

    def _build_grid(self):
        grid: dict[tuple[int, int, int], list[System]] = {}
        for s in self.systems.values():
            key = (int(s.x // _GRID_CELL), int(s.y // _GRID_CELL), int(s.z // _GRID_CELL))
            grid.setdefault(key, []).append(s)
        self._grid = grid

    # -- construction -------------------------------------------------------
    @classmethod
    def load(cls, progress=None) -> "Universe":
        if not SDE_CSV_PATH.exists():
            download_sde(progress=progress)
        if not config.MAP_REGIONS_PATH.exists():
            download_file(config.MAP_REGIONS_URL, config.MAP_REGIONS_PATH,
                          "region labels", progress)
        if not config.MAP_JUMPS_PATH.exists():
            download_file(config.MAP_JUMPS_URL, config.MAP_JUMPS_PATH,
                          "stargate network", progress)
        systems = _parse_csv(SDE_CSV_PATH)
        regions, region_names = _parse_regions(config.MAP_REGIONS_PATH)
        gates = _parse_gates(config.MAP_JUMPS_PATH)
        return cls(systems, regions, gates, region_names)

    # -- Ansiblex jump gates ------------------------------------------------
    def set_bridges(self, pairs) -> list[list[str]]:
        """Install Ansiblex links from [nameA, nameB] pairs.

        Returns the pairs that resolved, so callers can report bad names.
        """
        bridges: dict[int, set[int]] = {}
        resolved: list[list[str]] = []
        for pair in pairs or ():
            if len(pair) != 2:
                continue
            a, b = self.match_system(pair[0]), self.match_system(pair[1])
            if a is None or b is None or a.id == b.id:
                continue
            bridges.setdefault(a.id, set()).add(b.id)
            bridges.setdefault(b.id, set()).add(a.id)
            resolved.append([a.name, b.name])
        self.bridges = bridges
        return resolved

    def long_gates(self, min_ly: float):
        """Stargate links spanning at least ``min_ly`` light years.

        These are the 'regional gates' worth knowing about: a single gate hop
        that covers more ground than a jump drive can reach.
        """
        out = []
        seen: set[tuple[int, int]] = set()
        for a_id, neighbours in self.gates.items():
            a = self.systems.get(a_id)
            if a is None:
                continue
            for b_id in neighbours:
                pair = (a_id, b_id) if a_id < b_id else (b_id, a_id)
                if pair in seen:
                    continue
                seen.add(pair)
                b = self.systems.get(b_id)
                if b is None:
                    continue
                d = self.distance_ly(a, b)
                if d >= min_ly:
                    out.append((a, b, d))
        out.sort(key=lambda t: -t[2])
        return out

    # -- NPC stations (lazy) ------------------------------------------------
    def load_stations(self, progress=None) -> None:
        if self.stations:
            return
        if not config.STA_STATIONS_PATH.exists():
            download_file(config.STA_STATIONS_URL, config.STA_STATIONS_PATH,
                          "station list", progress)
        if not config.INV_TYPES_PATH.exists():
            download_file(config.INV_TYPES_URL, config.INV_TYPES_PATH,
                          "type names", progress)
        type_names = _load_station_type_names(config.INV_TYPES_PATH)
        stations = _parse_stations(config.STA_STATIONS_PATH, type_names)
        self.station_type_names = type_names
        self.stations = stations
        by_sys: dict[int, list[Station]] = {}
        for st in stations.values():
            by_sys.setdefault(st.system_id, []).append(st)
        self.system_stations = by_sys

    # -- lookup -------------------------------------------------------------
    def match_system(self, text: str) -> "System | None":
        """Resolve a name that may carry trailing junk (e.g. an Ansiblex
        endpoint). Tries the whole string, then the longest leading run of
        words that names a real system -- so "Old Man Star" still matches."""
        text = (text or "").strip()
        if not text:
            return None
        exact = self.by_name(text)
        if exact:
            return exact
        parts = text.split()
        for n in range(len(parts), 0, -1):
            found = self.by_name(" ".join(parts[:n]))
            if found:
                return found
        return None

    def by_name(self, name: str) -> System | None:
        return self._by_name.get(name.strip().lower())

    def search(self, text: str, limit: int = 25) -> list[System]:
        text = text.strip().lower()
        if not text:
            return []
        starts = [s for s in self.systems.values() if s.name.lower().startswith(text)]
        starts.sort(key=lambda s: s.name)
        if len(starts) >= limit:
            return starts[:limit]
        contains = [
            s for s in self.systems.values()
            if text in s.name.lower() and not s.name.lower().startswith(text)
        ]
        contains.sort(key=lambda s: s.name)
        return (starts + contains)[:limit]

    # -- geometry -----------------------------------------------------------
    @staticmethod
    def distance_ly(a: System, b: System) -> float:
        return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)

    def within_range(self, origin: System, range_ly: float, jumpable_only: bool = True) -> list[tuple[System, float]]:
        out: list[tuple[System, float]] = []
        ox, oy, oz = origin.x, origin.y, origin.z
        r2 = range_ly * range_ly
        cx = int(ox // _GRID_CELL)
        cy = int(oy // _GRID_CELL)
        cz = int(oz // _GRID_CELL)
        span = int(range_ly // _GRID_CELL) + 1
        grid = self._grid
        for ix in range(cx - span, cx + span + 1):
            for iy in range(cy - span, cy + span + 1):
                for iz in range(cz - span, cz + span + 1):
                    for s in grid.get((ix, iy, iz), ()):
                        if s.id == origin.id:
                            continue
                        if jumpable_only and not s.jumpable:
                            continue
                        d2 = (s.x - ox) ** 2 + (s.y - oy) ** 2 + (s.z - oz) ** 2
                        if d2 <= r2:
                            out.append((s, math.sqrt(d2)))
        out.sort(key=lambda t: t[1])
        return out

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(min_x, min_z, max_x, max_z) in light years, for map fitting."""
        xs = [s.x for s in self.systems.values()]
        zs = [s.z for s in self.systems.values()]
        return min(xs), min(zs), max(xs), max(zs)


def _parse_csv(path) -> dict[int, System]:
    systems: dict[int, System] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                region_id = int(row["regionID"])
                if region_id >= _KSPACE_MAX_REGION:
                    continue
                sid = int(row["solarSystemID"])
                systems[sid] = System(
                    id=sid,
                    name=row["solarSystemName"],
                    x=float(row["x"]) / LY_METERS,
                    y=float(row["y"]) / LY_METERS,
                    z=float(row["z"]) / LY_METERS,
                    security=round(float(row["security"]), 2),
                    region_id=region_id,
                    constellation_id=int(row["constellationID"]),
                )
            except (KeyError, ValueError):
                continue
    if not systems:
        raise RuntimeError("SDE parse produced no systems; the CSV format may have changed.")
    return systems


def _parse_regions(path):
    labels: list[tuple[str, float, float]] = []
    names: dict[int, str] = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                try:
                    rid = int(row["regionID"])
                    names[rid] = row["regionName"]
                    if rid >= _KSPACE_MAX_REGION:
                        continue
                    labels.append((row["regionName"],
                                   float(row["x"]) / LY_METERS,
                                   float(row["z"]) / LY_METERS))
                except (KeyError, ValueError):
                    continue
    except OSError:
        pass
    return labels, names


def _parse_gates(path) -> dict[int, set[int]]:
    gates: dict[int, set[int]] = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                try:
                    a = int(row["fromSolarSystemID"])
                    b = int(row["toSolarSystemID"])
                except (KeyError, ValueError):
                    continue
                gates.setdefault(a, set()).add(b)
                gates.setdefault(b, set()).add(a)
    except OSError:
        pass
    return gates


def download_file(url, path, label: str = "data", progress=None) -> None:
    if progress:
        progress(f"Downloading {label} from Fuzzwork...")
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            fh.write(chunk)
    tmp.replace(path)


def download_sde(progress=None) -> None:
    """Download the mapSolarSystems CSV to the cache directory."""
    download_file(SDE_CSV_URL, SDE_CSV_PATH, "New Eden map data", progress)
    if progress:
        progress("Map data ready.")


def _load_station_type_names(path) -> dict[int, str]:
    """typeID -> typeName for station types (groupID 15)."""
    names: dict[int, str] = {}
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                if int(row["groupID"]) == 15:  # 15 = Station
                    names[int(row["typeID"])] = row["typeName"]
            except (KeyError, ValueError):
                continue
    return names


def _parse_stations(path, type_names: dict[int, str]) -> dict[int, Station]:
    stations: dict[int, Station] = {}
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                sid = int(row["stationID"])
                type_id = int(row["stationTypeID"])
                stations[sid] = Station(
                    id=sid,
                    name=row["stationName"],
                    system_id=int(row["solarSystemID"]),
                    type_id=type_id,
                    type_name=type_names.get(type_id, ""),
                    max_volume=float(row.get("maxShipVolumeDockable") or 0.0),
                )
            except (KeyError, ValueError):
                continue
    return stations
