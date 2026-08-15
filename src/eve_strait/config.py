"""Paths, ESI endpoints and physical constants."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple

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
    "esi-location.read_ship_type.v1",
    "esi-ui.write_waypoint.v1",
    "esi-search.search_structures.v1",
    "esi-characters.read_contacts.v1",
    "esi-corporations.read_contacts.v1",
    "esi-alliances.read_contacts.v1",
    "esi-corporations.read_structures.v1",
    "esi-corporations.read_starbases.v1",
]


class ScopeInfo(NamedTuple):
    """One scope, described by what it turns on rather than by its name."""
    scope: str
    title: str        # the feature it buys, in the app's own terms
    detail: str       # what is read or written to provide it
    risk: str = ""    # what someone learns if they see it; "" = nothing
    required: bool = False


# Grouped by the question a player is actually asking - "what does this let the
# app do, and what does it tell anyone about me". The risk lines are the point:
# the feedback that prompted this was that people are fine with most of these
# and specifically wary of the ones that amount to live intel, so the dialog has
# to say which is which instead of listing eleven identical-looking strings.
SCOPE_GROUPS: list[tuple[str, str, list[ScopeInfo]]] = [
    ("Always", "Needed to sign in at all.", [
        ScopeInfo("publicData",
                  "Sign in",
                  "Confirms which character you are. Nothing else.",
                  required=True),
    ]),
    ("Your stations and structures",
     "How the planner knows where you can actually dock.", [
        ScopeInfo("esi-assets.read_assets.v1",
                  "Where your assets are",
                  "Reads your asset list to find stations and citadels you "
                  "keep things in.",
                  "This is your full asset list - everything you own and "
                  "where. The most revealing scope here."),
        ScopeInfo("esi-universe.read_structures.v1",
                  "Name private citadels",
                  "Turns structure IDs into names and systems. Without it, "
                  "player structures show as bare numbers.",
                  "Reveals which private structures you have access to."),
        ScopeInfo("esi-search.search_structures.v1",
                  "Find Ansiblex gates",
                  "Searches structures you can already see, to fill in jump "
                  "bridges by name."),
    ]),
    ("Live position", "", [
        ScopeInfo("esi-location.read_location.v1",
                  "Use your current system as the origin",
                  "Reads the system your character is in right now.",
                  "Real-time location. Read while you are docked or in space, "
                  "and it is current to the second."),
        ScopeInfo("esi-location.read_ship_type.v1",
                  "Find your cyno alts",
                  "Reads which ship each character is sitting in, so the app "
                  "can tell you which of them has a cyno fitted and where it "
                  "is parked. Needs the asset scope above as well, since that "
                  "is what says which modules are fitted.",
                  "The ship you are flying, by name and hull."),
        ScopeInfo("esi-ui.write_waypoint.v1",
                  "Set the route in your game client",
                  "Writes waypoints to your in-game autopilot. The only scope "
                  "here that writes anything.",
                  "Can change your autopilot destination. It cannot fly, "
                  "dock, trade or fit anything."),
    ]),
    ("Standings", "Used to route around people who dislike you.", [
        ScopeInfo("esi-characters.read_contacts.v1",
                  "Your personal contacts",
                  "Reads your own contact list and standings."),
        ScopeInfo("esi-corporations.read_contacts.v1",
                  "Your corporation's contacts",
                  "Reads your corp's contact list."),
        ScopeInfo("esi-alliances.read_contacts.v1",
                  "Your alliance's contacts",
                  "Reads your alliance's contact list."),
    ]),
    ("Corporation assets",
     "Both need an in-game role as well as the scope. Without the role ESI "
     "returns 403 and the app carries on without them.", [
        ScopeInfo("esi-corporations.read_structures.v1",
                  "Corp structures you can dock in",
                  "Reads your corporation's structure list. Needs the Station "
                  "Manager role.",
                  "Your corp's full structure list, including locations."),
        ScopeInfo("esi-corporations.read_starbases.v1",
                  "Corp starbases to park a capital at",
                  "Reads your corporation's POS list. Needs the Director "
                  "role.",
                  "Your corp's full starbase list, including locations."),
    ]),
]


def scope_catalogue() -> list[ScopeInfo]:
    return [s for _, _, group in SCOPE_GROUPS for s in group]

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


# A default so the app works the moment you sign in, without every user
# first creating their own EVE application. Not a secret: this app uses
# OAuth2 PKCE specifically so no client secret is ever needed, and a Client
# ID is meant to be public -- EVE's own SSO design assumes it ends up in
# client-side code. Overridable in Settings; get_client_id() below is the
# only thing that should ever see this value. The Settings field itself
# reads get_custom_client_id() instead, which leaves it blank rather than
# display the default -- Settings should show what you configured, not the
# fallback quietly working behind it.
DEFAULT_CLIENT_ID = "ece9f8ae563f4a22b148d719749dc29d"


def get_client_id() -> str | None:
    return (os.environ.get("EVE_CLIENT_ID") or load_config().get("client_id")
            or DEFAULT_CLIENT_ID)


def get_custom_client_id() -> str:
    """The user's own override, if any -- never the built-in default.

    For anything that decides what to SHOW (the Settings field) or what
    counts as "did this change" (whether to save a new one). get_client_id()
    is for anything that needs a value to actually sign in with.

    The final "was that actually the default" check is deliberate belt and
    suspenders, not redundant with the callers already passing this instead
    of get_client_id(): if the default ever ends up saved to config.json as
    though it were a real override -- an old build that pre-filled the field
    with it before this existed, someone hand-editing the file, anything --
    this is the one place that guarantees it still reads back as unset rather
    than quietly starting to show the default as if the user had chosen it.
    """
    v = os.environ.get("EVE_CLIENT_ID") or load_config().get("client_id") or ""
    return "" if v == DEFAULT_CLIENT_ID else v


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


def get_saved_routes() -> dict:
    """name -> {"systems": [system names], "ship": <ship key or None>}.

    System *names* rather than IDs, so a saved route stays readable in
    config.json and survives an SDE refresh.
    """
    routes = load_config().get("saved_routes", {})
    return routes if isinstance(routes, dict) else {}


def save_route(name: str, systems: list[str], ship: str | None = None) -> None:
    cfg = load_config()
    routes = cfg.get("saved_routes", {})
    if not isinstance(routes, dict):
        routes = {}
    routes[name] = {"systems": list(systems), "ship": ship}
    cfg["saved_routes"] = routes
    save_config(cfg)


def delete_route(name: str) -> None:
    cfg = load_config()
    routes = cfg.get("saved_routes", {})
    if isinstance(routes, dict) and routes.pop(name, None) is not None:
        cfg["saved_routes"] = routes
        save_config(cfg)


def get_system_notes() -> dict:
    """system name -> free text ("gate camp", "friendly Fortizar").

    Keyed by name rather than ID so the file stays readable and survives an
    SDE refresh, same as saved routes and the avoid list.
    """
    notes = load_config().get("system_notes", {})
    return notes if isinstance(notes, dict) else {}


def set_system_note(system_name: str, text: str | None) -> None:
    cfg = load_config()
    notes = cfg.get("system_notes", {})
    if not isinstance(notes, dict):
        notes = {}
    text = (text or "").strip()
    if text:
        notes[system_name] = text
    else:
        notes.pop(system_name, None)     # empty text means delete the note
    cfg["system_notes"] = notes
    save_config(cfg)


# ---------------------------------------------------------------------------
# AI assistant. Everything here is off until the user opts in.
#
# Anything the assistant reads can be sent to Anthropic or OpenAI: system
# names, your notes, your route, and (if you enable it) structure names and
# standings. That is EVE intelligence, so none of it leaves the machine
# without an explicit choice.
# ---------------------------------------------------------------------------
def get_ai_provider() -> str:
    return load_config().get("ai_provider", "claude")


def set_ai_provider(name: str) -> None:
    cfg = load_config()
    cfg["ai_provider"] = name
    save_config(cfg)


def get_ai_key(provider: str) -> str:
    """API key for one provider, or "" if none is set.

    Env var wins, so a key can be supplied without ever writing it to disk.
    """
    # The variable name lives with the provider definition, so adding a
    # provider does not mean remembering to update a map over here.
    from .ai import providers
    _env_name = providers.env_var(provider)
    env = os.environ.get(_env_name) if _env_name else ""
    if env:
        return env
    return (load_config().get("ai_keys") or {}).get(provider, "")


def set_ai_key(provider: str, key: str) -> None:
    cfg = load_config()
    keys = cfg.get("ai_keys") or {}
    key = (key or "").strip()
    if key:
        keys[provider] = key
    else:
        keys.pop(provider, None)
    cfg["ai_keys"] = keys
    save_config(cfg)


def get_ai_model(provider: str) -> str:
    return (load_config().get("ai_models") or {}).get(provider, "")


def set_ai_model(provider: str, model: str) -> None:
    cfg = load_config()
    models = cfg.get("ai_models") or {}
    models[provider] = model
    save_config(cfg)


def ai_configured() -> bool:
    """Whether any provider has a key. The chat panel does not exist until
    this is true, so an unconfigured install has no AI surface at all."""
    from .ai import providers
    return any(get_ai_key(p) for p in providers.names())


def get_ai_chat_enabled() -> bool:
    """Whether the in-app chat panel is allowed to exist at all.

    Separate from having a key: a key answers "could this work", this
    answers "should it show up". Someone who pasted a key once to try the
    assistant and decided against it should not have to delete the key
    itself just to make the panel go away, and shouldn't need to remember to
    re-paste it later either.
    """
    return bool(load_config().get("ai_chat_enabled", True))


def set_ai_chat_enabled(on: bool) -> None:
    cfg = load_config()
    cfg["ai_chat_enabled"] = bool(on)
    save_config(cfg)


def get_mcp_enabled() -> bool:
    """MCP server opt-in. Off by default and checked by the server itself,
    so an accidental launch refuses to serve rather than exposing tools."""
    return bool(load_config().get("mcp_enabled", False))


def set_mcp_enabled(on: bool) -> None:
    cfg = load_config()
    cfg["mcp_enabled"] = bool(on)
    save_config(cfg)


def get_mcp_allow_writes() -> bool:
    """Whether MCP tools may change anything. Separate opt-in from enabling
    the server, because reading intel and editing the avoid list are very
    different levels of trust to hand a model."""
    return bool(load_config().get("mcp_allow_writes", False))


def set_mcp_allow_writes(on: bool) -> None:
    cfg = load_config()
    cfg["mcp_allow_writes"] = bool(on)
    save_config(cfg)


def get_mcp_allow_private() -> bool:
    """Whether MCP may expose ESI-authenticated data: your characters, their
    locations, structure names and standings. This is the real intel leak, so
    it is its own opt-in and defaults off even when the server is on."""
    return bool(load_config().get("mcp_allow_private", False))


def set_mcp_allow_private(on: bool) -> None:
    cfg = load_config()
    cfg["mcp_allow_private"] = bool(on)
    save_config(cfg)


# ---------------------------------------------------------------------------
# Wanderer map (wanderer.ltd). Self-hosted, so the instance URL, the map and
# its token all have to come from the user; there is nothing to hard-code.
# The token is a per-map key, not an account credential.
# ---------------------------------------------------------------------------
def get_wanderer_url() -> str:
    return (load_config().get("wanderer_url") or "").strip()


def get_wanderer_map() -> str:
    """Map slug or UUID, whichever the user pasted."""
    return (load_config().get("wanderer_map") or "").strip()


def get_wanderer_token() -> str:
    env = os.environ.get("WANDERER_TOKEN")
    return env or (load_config().get("wanderer_token") or "")


def set_wanderer(url: str, map_id: str, token: str) -> None:
    cfg = load_config()
    cfg["wanderer_url"] = (url or "").strip().rstrip("/")
    cfg["wanderer_map"] = (map_id or "").strip()
    token = (token or "").strip()
    if token:
        cfg["wanderer_token"] = token
    else:
        cfg.pop("wanderer_token", None)
    save_config(cfg)


def wanderer_configured() -> bool:
    return bool(get_wanderer_url() and get_wanderer_map() and get_wanderer_token())


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
