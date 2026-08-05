"""Long-term intel history, for looking at how a system changes over time.

`activity_history.py` keeps a 24 hour rolling window in JSON, which is all the
"last 24h" columns need. This module is the durable counterpart: an append-only
SQLite table so trends can be asked for later ("was this system always this
quiet, or did it die last week?").

SQLite rather than JSON because the volume is real. Roughly 5,000 systems
report gate traffic every hour, so a day is ~120k rows and a month is a few
million. That is comfortable for SQLite and miserable for a JSON blob.

Storage is opt-in via a retention setting: 0 days means record nothing.
Only rows with something happening are written; empty systems are skipped.
"""
from __future__ import annotations

import sqlite3
import time

from .. import config

DB_PATH = config.CACHE_DIR / "intel_history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    t          INTEGER NOT NULL,   -- unix seconds of the sample
    system_id  INTEGER NOT NULL,
    jumps      INTEGER DEFAULT 0,  -- gate traffic, last hour
    ship_kills INTEGER DEFAULT 0,
    pod_kills  INTEGER DEFAULT 0,
    npc_kills  INTEGER DEFAULT 0,  -- ratting
    adm        REAL                -- sovereignty defense multiplier
);
CREATE INDEX IF NOT EXISTS idx_samples_system_t ON samples (system_id, t);
CREATE INDEX IF NOT EXISTS idx_samples_t ON samples (t);
"""


def _connect() -> sqlite3.Connection | None:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.executescript(_SCHEMA)
        return conn
    except sqlite3.Error:
        return None


def enabled() -> bool:
    return config.get_intel_history_days() > 0


def record(activity: dict, defense: dict | None = None,
           when: float | None = None) -> int:
    """Persist one snapshot. Returns the number of rows written.

    A no-op when retention is set to 0, so the feature costs nothing until
    someone turns it on.
    """
    if not enabled():
        return 0
    kills = (activity or {}).get("kills") or {}
    jumps = (activity or {}).get("jumps") or {}
    defense = defense or {}
    if not kills and not jumps and not defense:
        return 0

    stamp = int(when or time.time())
    conn = _connect()
    if conn is None:
        return 0

    system_ids = set(kills) | set(jumps) | set(defense)
    rows = []
    for sid in system_ids:
        k = kills.get(sid) or {}
        ship, pod, npc = k.get("ship", 0), k.get("pod", 0), k.get("npc", 0)
        j = jumps.get(sid, 0)
        adm = (defense.get(sid) or {}).get("adm")
        if not (j or ship or pod or npc or adm is not None):
            continue          # nothing happened here; do not store a zero row
        rows.append((stamp, sid, j, ship, pod, npc, adm))

    try:
        with conn:
            # One sample per hour: drop anything already stored for this stamp.
            conn.execute("DELETE FROM samples WHERE t = ?", (stamp,))
            conn.executemany(
                "INSERT INTO samples (t, system_id, jumps, ship_kills, "
                "pod_kills, npc_kills, adm) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            cutoff = stamp - config.get_intel_history_days() * 86400
            conn.execute("DELETE FROM samples WHERE t < ?", (cutoff,))
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
    return len(rows)


def history(system_id: int, days: int = 30) -> list[dict]:
    """Every stored sample for one system, oldest first."""
    conn = _connect()
    if conn is None:
        return []
    cutoff = int(time.time()) - days * 86400
    try:
        cur = conn.execute(
            "SELECT t, jumps, ship_kills, pod_kills, npc_kills, adm "
            "FROM samples WHERE system_id = ? AND t >= ? ORDER BY t",
            (int(system_id), cutoff))
        return [{"t": r[0], "jumps": r[1], "ship_kills": r[2],
                 "pod_kills": r[3], "npc_kills": r[4], "adm": r[5]}
                for r in cur.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def stats() -> dict:
    """Row count, span and file size, for the settings dialog."""
    out = {"rows": 0, "systems": 0, "oldest": None, "newest": None,
           "size_mb": 0.0}
    try:
        out["size_mb"] = DB_PATH.stat().st_size / 1048576 if DB_PATH.exists() else 0.0
    except OSError:
        pass
    conn = _connect()
    if conn is None:
        return out
    try:
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT system_id), MIN(t), MAX(t) "
            "FROM samples").fetchone()
        if row:
            out.update(rows=row[0] or 0, systems=row[1] or 0,
                       oldest=row[2], newest=row[3])
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def purge() -> None:
    """Delete all stored history."""
    conn = _connect()
    if conn is None:
        return
    try:
        with conn:
            conn.execute("DELETE FROM samples")
        conn.execute("VACUUM")
    except sqlite3.Error:
        pass
    finally:
        conn.close()
