"""Paths, ESI endpoints and physical constants."""
from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "eve-jump-planner"

# ---------------------------------------------------------------------------
# Filesystem locations
# ---------------------------------------------------------------------------
def _data_home() -> Path:
    # Windows: %LOCALAPPDATA%, otherwise ~/.local/share
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


DATA_DIR = _data_home()
CACHE_DIR = DATA_DIR / "cache"
CONFIG_PATH = DATA_DIR / "config.json"
TOKEN_PATH = DATA_DIR / "token.json"
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
