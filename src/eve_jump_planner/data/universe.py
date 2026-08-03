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
                 gates: dict[int, set[int]] | None = None):
        self.systems = systems
        self._by_name = {s.name.lower(): s for s in systems.values()}
        # (name, x_ly, z_ly) region label anchors.
        self.regions = regions or []
        # Stargate adjacency: system_id -> set of gate-connected system_ids.
        self.gates = gates or {}
        # Populated lazily by load_stations().
        self.stations: dict[int, Station] = {}
        self.system_stations: dict[int, list[Station]] = {}
        self.station_type_names: dict[int, str] = {}

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
        regions = _parse_regions(config.MAP_REGIONS_PATH)
        gates = _parse_gates(config.MAP_JUMPS_PATH)
        return cls(systems, regions, gates)

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
        for s in self.systems.values():
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


def _parse_regions(path) -> list[tuple[str, float, float]]:
    out: list[tuple[str, float, float]] = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                try:
                    if int(row["regionID"]) >= _KSPACE_MAX_REGION:
                        continue
                    out.append((row["regionName"],
                                float(row["x"]) / LY_METERS,
                                float(row["z"]) / LY_METERS))
                except (KeyError, ValueError):
                    continue
    except OSError:
        pass
    return out


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
