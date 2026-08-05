"""Rolling 24 hour history of system activity, accumulated locally.

ESI only ever reports the **last hour**: `/universe/system_jumps/` and
`/universe/system_kills/` are one-hour snapshots with no history endpoint.
There is no 24 hour figure to fetch, so the only way to get one is to keep
samples ourselves.

That means the 24 hour totals are honest but partial: they cover the hours the
app was actually running. `coverage_hours()` reports how many distinct hourly
buckets we hold, so the UI can say so rather than implying full coverage.

Samples are de-duplicated on the snapshot's `expires` header, which changes
once per hour, so polling repeatedly inside one hour never double counts.
"""
from __future__ import annotations

import json
import time

from .. import config

HISTORY_PATH = config.CACHE_DIR / "activity_history.json"
_WINDOW_SECONDS = 24 * 3600
_MAX_SAMPLES = 30          # a little over 24h of hourly buckets


def _load() -> list[dict]:
    try:
        data = json.loads(HISTORY_PATH.read_text("utf-8"))
        return data.get("samples", []) if isinstance(data, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(samples: list[dict]) -> None:
    try:
        HISTORY_PATH.write_text(json.dumps({"samples": samples}), "utf-8")
    except OSError:
        pass


def record(activity: dict) -> None:
    """Append one hourly snapshot, ignoring a repeat of the same hour."""
    kills = activity.get("kills") or {}
    jumps = activity.get("jumps") or {}
    if not kills and not jumps:
        return
    expires = activity.get("expires") or ""
    samples = _load()
    if expires and any(s.get("expires") == expires for s in samples):
        return                      # same ESI hour bucket, already recorded

    samples.append({
        "t": time.time(),
        "expires": expires,
        # Store only non-zero entries; most of New Eden is quiet.
        "jumps": {str(k): v for k, v in jumps.items() if v},
        "kills": {str(k): [v.get("ship", 0), v.get("pod", 0)]
                  for k, v in kills.items() if v.get("ship") or v.get("pod")},
    })
    cutoff = time.time() - _WINDOW_SECONDS
    samples = [s for s in samples if s.get("t", 0) >= cutoff][-_MAX_SAMPLES:]
    _save(samples)


def totals() -> dict:
    """Summed activity over the retained window.

    Returns {"jumps": {system_id: int}, "kills": {system_id: {"ship","pod"}},
             "hours": int} where ``hours`` is how many hourly samples we hold.
    """
    jumps: dict[int, int] = {}
    kills: dict[int, dict] = {}
    samples = _load()
    for s in samples:
        for sid, n in (s.get("jumps") or {}).items():
            jumps[int(sid)] = jumps.get(int(sid), 0) + n
        for sid, pair in (s.get("kills") or {}).items():
            slot = kills.setdefault(int(sid), {"ship": 0, "pod": 0})
            slot["ship"] += pair[0] if isinstance(pair, list) else 0
            slot["pod"] += pair[1] if isinstance(pair, list) and len(pair) > 1 else 0
    return {"jumps": jumps, "kills": kills, "hours": len(samples)}


def coverage_hours() -> int:
    """How many hourly samples we actually hold (max 24)."""
    return len(_load())
