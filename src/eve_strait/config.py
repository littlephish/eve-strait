"""Paths, ESI endpoints and physical constants."""
from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "eve-strait"

# ---------------------------------------------------------------------------
# Filesystem locations
# ---------------------------------------------------------------------------
def _data_home() -> Path:
    # Windows: %LOCALAPPDATA%, otherwise ~/.local/share
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def _migrate_legacy_dir(new_dir: Path) -> None:
    """Carry settings over from the pre-rename data directory.

    The app used to be called "eve-jump-planner"; without this the rename
    would silently orphan the saved Client ID, token, pinned docks and cache.
    Copies rather than renames: a directory rename fails on Windows if any
    file inside is open, and copying leaves the old data intact as a backup.
    """
    legacy = new_dir.parent / "eve-jump-planner"
    if not legacy.is_dir():
        return
    # Only real settings count as "already migrated". An empty cache/ folder
    # auto-created by a previous import must not block the copy.
    if (new_dir / "config.json").exists() or (new_dir / "token.json").exists():
        return
    try:
        import shutil
        shutil.copytree(legacy, new_dir, dirs_exist_ok=True)
    except OSError:
        pass


DATA_DIR = _data_home()
_migrate_legacy_dir(DATA_DIR)
CACHE_DIR = DATA_DIR / "cache"
CONFIG_PATH = DATA_DIR / "config.json"
TOKEN_PATH = DATA_DIR / "token.json"          # legacy single-character store
TOKENS_PATH = DATA_DIR / "tokens.json"        # multi-character store
SDE_CSV_PATH = CACHE_DIR / "mapSolarSystems.csv"
MAP_REGIONS_PATH = CACHE_DIR / "mapRegions.csv"
MAP_JUMPS_PATH = CACHE_DIR / "mapSolarSystemJumps.csv"
STA_STATIONS_PATH = CACHE_DIR / "staStations.csv"
INV_TYPES_PATH = CACHE_DIR / "invTypes.csv"

for _d in (DATA_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# ESI / SSO endpoints
# ---------------------------------------------------------------------------
ESI_BASE = "https://esi.evetech.net/latest"
SSO_AUTHORIZE = "https://login.eveonline.com/v2/oauth/authorize"
SSO_TOKEN = "https://login.eveonline.com/v2/oauth/token"
SSO_ISSUER = "login.eveonline.com"

# The local callback server port. Your EVE application's Callback URL must be
# set to EXACTLY this value on developers.eveonline.com:
CALLBACK_PORT = 8635
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"

SCOPES = [
    "publicData",
    "esi-assets.read_assets.v1",
    "esi-universe.read_structures.v1",
    "esi-location.read_location.v1",
    "esi-ui.write_waypoint.v1",
    "esi-search.search_structures.v1",
    "esi-characters.read_contacts.v1",
    "esi-corporations.read_contacts.v1",
    "esi-alliances.read_contacts.v1",
    "esi-corporations.read_structures.v1",
    "esi-corporations.read_starbases.v1",
]

# Fuzzwork Static Data Export dumps.
SDE_CSV_URL = "https://www.fuzzwork.co.uk/dump/latest/csv/mapSolarSystems.csv"
MAP_REGIONS_URL = "https://www.fuzzwork.co.uk/dump/latest/csv/mapRegions.csv"
MAP_JUMPS_URL = "https://www.fuzzwork.co.uk/dump/latest/csv/mapSolarSystemJumps.csv"
STA_STATIONS_URL = "https://www.fuzzwork.co.uk/dump/latest/csv/staStations.csv"
INV_TYPES_URL = "https://www.fuzzwork.co.uk/dump/latest/csv/invTypes.csv"

# ---------------------------------------------------------------------------
# Physics
# ---------------------------------------------------------------------------
# One (Julian) light year in metres, as EVE uses it.
LY_METERS = 9_460_730_472_580_800.0

# Jump drives cannot be activated into high-security space (>= 0.5).
JUMPABLE_SECURITY_MAX = 0.5


# ---------------------------------------------------------------------------
# User config (client id lives here or in the EVE_CLIENT_ID env var)
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_client_id() -> str | None:
    return os.environ.get("EVE_CLIENT_ID") or load_config().get("client_id")


def get_default_docks() -> dict:
    """system_id (str) -> dock name the user pinned for that system."""
    return load_config().get("default_docks", {})


def set_default_dock(system_id: int, dock_name: str | None) -> None:
    cfg = load_config()
    docks = cfg.get("default_docks", {})
    if dock_name:
        docks[str(system_id)] = dock_name
    else:
        docks.pop(str(system_id), None)
    cfg["default_docks"] = docks
    save_config(cfg)


def get_bridges() -> list[list[str]]:
    """Ansiblex jump-gate links as [systemA, systemB] name pairs."""
    return load_config().get("bridges", [])


def set_bridges(pairs: list[list[str]]) -> None:
    cfg = load_config()
    cfg["bridges"] = pairs
    save_config(cfg)


def get_docking_rights() -> list[str]:
    """Corporation / alliance names whose structures you may dock at,
    regardless of standing (rentals, NAPs, blue-in-practice deals)."""
    return load_config().get("docking_rights", [])


def set_docking_rights(names: list[str]) -> None:
    cfg = load_config()
    cfg["docking_rights"] = names
    save_config(cfg)


def get_intel_refresh_minutes() -> int:
    """How often to re-poll activity, ADM and industry data. 0 = never.

    The underlying ESI snapshots are cached for an hour, so polling faster
    than that returns the same numbers; 60 is the useful default.
    """
    try:
        return int(load_config().get("intel_refresh_minutes", 60))
    except (TypeError, ValueError):
        return 60


def set_intel_refresh_minutes(minutes: int) -> None:
    cfg = load_config()
    cfg["intel_refresh_minutes"] = max(0, int(minutes))
    save_config(cfg)


def get_intel_history_days() -> int:
    """How many days of intel samples to keep on disk. 0 = don't store any.

    Off by default: the history database grows by roughly a million rows a
    week, so it should be an explicit choice.
    """
    try:
        return int(load_config().get("intel_history_days", 0))
    except (TypeError, ValueError):
        return 0


def set_intel_history_days(days: int) -> None:
    cfg = load_config()
    cfg["intel_history_days"] = max(0, int(days))
    save_config(cfg)


def get_avoided() -> list[str]:
    """System names the router must never route through."""
    return load_config().get("avoid", [])


def set_avoided(names: list[str]) -> None:
    cfg = load_config()
    cfg["avoid"] = sorted(set(names))
    save_config(cfg)


def get_settings() -> dict:
    """Persisted UI state (selected ship, skill levels, toggles)."""
    return load_config().get("settings", {})


def save_settings(settings: dict) -> None:
    cfg = load_config()
    cfg["settings"] = settings
    save_config(cfg)


def parse_scopes(text: str) -> list[str]:
    """Accept a JSON array (as copied from developers.eveonline.com) OR a
    space/comma/newline-separated list."""
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(s).strip() for s in data if str(s).strip()]
        except json.JSONDecodeError:
            pass
    return [s for s in text.replace(",", " ").split() if s]


def get_scopes() -> list[str]:
    """Scopes to request. Override in config.json ("scopes": [...]) or the
    EVE_SCOPES env var to exactly match what your EVE application has granted."""
    cfg = load_config()
    if cfg.get("scopes"):
        return list(cfg["scopes"])
    env = os.environ.get("EVE_SCOPES")
    if env:
        return parse_scopes(env)
    return list(SCOPES)


def set_scopes(scopes: list[str]) -> None:
    cfg = load_config()
    cfg["scopes"] = scopes
    save_config(cfg)
