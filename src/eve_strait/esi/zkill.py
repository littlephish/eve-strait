"""Cyno activity proxy, via zKillboard killmails.

READ THIS BEFORE TRUSTING THE NUMBER.

CCP does not expose cyno lightings. There is no cyno endpoint anywhere in
ESI, so a true "cynos lit here" count cannot be built. What this module
measures is the only observable trace: **ships that died with a cynosural
field generator fitted**.

That is a floor, not a count. Most cynos are never killed, so a system with
heavy cyno traffic and competent alts reports zero. Treat a non-zero number as
"capitals have been bridging here", and never present it as a cyno count.

Module type IDs come from SDE group 658 (Cynosural Field Generator).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .. import config

# SDE group 658: the fitted generators that light a cyno.
CYNO_MODULE_TYPE_IDS = {
    21096,  # Cynosural Field Generator I
    28646,  # Covert Cynosural Field Generator I
    52694,  # Industrial Cynosural Field Generator
}

_ZKILL = "https://zkillboard.com/api"
# zKillboard asks third-party tools to identify themselves and to stay under
# roughly one request per second.
_UA = "eve-strait/0.1 (+https://github.com/littlephish/eve-strait)"
_MIN_INTERVAL = 1.1
_last_request = 0.0

_cache: dict[int, tuple[float, dict]] = {}
_CACHE_TTL = 30 * 60


def _throttled_get(url: str, timeout: int = 30):
    global _last_request
    wait = _MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def cyno_losses(system_id: int, hours: int = 24, max_kills: int = 40,
                progress=None) -> dict:
    """Cyno-fitted ships destroyed in a system recently.

    Returns {"losses": int, "sampled": int, "capped": bool, "hours": int,
             "error": str}. ``capped`` means more kills existed than we
    inspected, so ``losses`` is an undercount of an already-undercounting
    proxy.
    """
    now = time.time()
    hit = _cache.get(system_id)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]

    result = {"losses": 0, "sampled": 0, "capped": False, "hours": hours,
              "error": ""}
    seconds = max(60, min(int(hours * 3600), 7 * 24 * 3600))  # zKill caps at 7d
    try:
        if progress:
            progress(f"Querying zKillboard for system {system_id}...")
        kills = _throttled_get(
            f"{_ZKILL}/solarSystemID/{int(system_id)}/pastSeconds/{seconds}/")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        result["error"] = f"zKillboard unavailable: {exc}"
        return result

    if not isinstance(kills, list):
        result["error"] = "unexpected zKillboard response"
        return result

    result["capped"] = len(kills) > max_kills
    for i, kill in enumerate(kills[:max_kills]):
        km_id = kill.get("killmail_id")
        km_hash = (kill.get("zkb") or {}).get("hash")
        if not km_id or not km_hash:
            continue
        if progress:
            progress(f"Checking killmail {i + 1}/{min(len(kills), max_kills)}...")
        try:
            # Full killmails are public on ESI; zKill only gives id + hash.
            km = _throttled_get(
                f"{config.ESI_BASE}/killmails/{km_id}/{km_hash}/")
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            continue
        result["sampled"] += 1
        items = (km.get("victim") or {}).get("items") or []
        if any(it.get("item_type_id") in CYNO_MODULE_TYPE_IDS for it in items):
            result["losses"] += 1

    _cache[system_id] = (now, result)
    return result


def _kill_pages(scope: str, ident: int, seconds: int, max_pages: int = 6):
    """Yield pages of full killmails for a zKill scope (regionID/solarSystemID).

    The region feed embeds the whole killmail, victim fittings included, so a
    region costs one request per 200 kills rather than one per killmail. That
    is the difference between a map-wide sweep taking minutes and taking days.
    """
    base = f"{_ZKILL}/kills/{scope}/{int(ident)}/pastSeconds/{seconds}"
    for page in range(1, max_pages + 1):
        url = f"{base}/page/{page}/" if page > 1 else f"{base}/"
        kills = _throttled_get(url, timeout=90)
        if not isinstance(kills, list) or not kills:
            return
        yield kills
        if len(kills) < 200:          # zKill's page size; short page = last page
            return


def sweep_regions(region_ids, hours: int = 24, progress=None,
                  should_stop=None) -> dict:
    """Count cyno-fitted losses per system across whole regions.

    Returns {"systems": {system_id: losses}, "kills": int, "regions": int,
             "hours": int, "partial": bool, "error": str}.

    Same caveat as everywhere else in this module: this counts cyno ships that
    *died*. It is a floor on cyno traffic, not a census of it.
    """
    region_ids = list(region_ids)
    seconds = max(60, min(int(hours * 3600), 7 * 24 * 3600))
    out = {"systems": {}, "kills": 0, "regions": 0, "hours": hours,
           "partial": False, "error": ""}

    for i, rid in enumerate(region_ids, 1):
        if should_stop and should_stop():
            out["partial"] = True
            break
        if progress:
            progress(f"Scanning region {i}/{len(region_ids)} for cyno losses "
                     f"({out['kills']:,} kills checked)...")
        try:
            for kills in _kill_pages("regionID", rid, seconds):
                out["kills"] += len(kills)
                for kill in kills:
                    sid = kill.get("solar_system_id")
                    items = (kill.get("victim") or {}).get("items") or []
                    if sid and any(it.get("item_type_id") in CYNO_MODULE_TYPE_IDS
                                   for it in items):
                        out["systems"][sid] = out["systems"].get(sid, 0) + 1
                if should_stop and should_stop():
                    out["partial"] = True
                    break
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            # One bad region should not throw away everything already scanned.
            out["error"] = f"zKillboard: {exc}"
            out["partial"] = True
            continue
        out["regions"] += 1
    return out


# -- persisted sweep results ------------------------------------------------
_STORE = config.CACHE_DIR / "cyno_activity.json"


def load_sweep() -> dict:
    """Last sweep result, so the layer survives a restart."""
    try:
        return json.loads(_STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_sweep(result: dict) -> None:
    data = dict(result)
    data["t"] = time.time()
    # JSON keys are strings; keep the caller's int keys usable on reload.
    data["systems"] = {str(k): v for k, v in (result.get("systems") or {}).items()}
    try:
        _STORE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def sweep_systems(data: dict) -> dict:
    """{system_id: losses} from a stored sweep, with int keys restored."""
    out = {}
    for k, v in (data.get("systems") or {}).items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def describe(result: dict) -> str:
    """One honest line for the UI."""
    if result.get("error"):
        return result["error"]
    n, hours = result["losses"], result["hours"]
    if n == 0:
        return (f"No cyno-fitted losses in the last {hours}h. "
                "Cynos are rarely killed, so this does not mean none were lit.")
    more = " (sample capped)" if result.get("capped") else ""
    return (f"{n} cyno-fitted ship loss(es) in the last {hours}h{more}. "
            "A floor, not a count: most cynos are never killed.")
