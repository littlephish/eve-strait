"""Disk cache for ESI responses, keyed by request identity.

sqlite rather than a file per entry: station and structure name resolution
can reach thousands of rows, and a cache directory with thousands of tiny
JSON files is hostile to both the filesystem and anyone reading it. sqlite3
is stdlib, so this adds no runtime dependency and survives the Nuitka build.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key         TEXT PRIMARY KEY,
    body        BLOB NOT NULL,
    etag        TEXT NOT NULL DEFAULT '',
    headers     TEXT NOT NULL DEFAULT '{}',
    fetched_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
)
"""


@dataclass(frozen=True)
class Freshness:
    fetched_at: float
    expires_at: float


@dataclass(frozen=True)
class Entry:
    body: bytes
    etag: str
    fetched_at: float
    expires_at: float
    headers: dict


def cache_key(method: str, url: str, params, character_id) -> str:
    """Stable key for one request identity.

    Hashed rather than stored raw: URLs with many params get long, and the
    key is a primary key we look up on every single call.
    """
    payload = json.dumps(
        [method.upper(), url, sorted((params or {}).items()), character_id],
        sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_expires(headers, now: float) -> float | None:
    """Absolute epoch time from an HTTP `expires` header. None if absent/junk.

    ESI sends a real HTTP date here; "0" and other cache-busting values are
    treated as absent rather than as "already expired", because the caller
    already handles a missing expiry.
    """
    raw = headers.get("expires") or headers.get("Expires")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError):
        return None


class HttpCache:
    """Thread-safe because Qt workers call ESI off the main thread."""

    def __init__(self, path: Path, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock:
            self._db.execute(_SCHEMA)
            self._db.commit()

    def get(self, key: str) -> Entry | None:
        """The stored entry, expired or not. Callers judge freshness."""
        with self._lock:
            row = self._db.execute(
                "SELECT body, etag, fetched_at, expires_at, headers FROM "
                "responses WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            headers = json.loads(row[4])
        except (TypeError, ValueError):
            headers = {}
        return Entry(row[0], row[1], row[2], row[3], headers)

    def put(self, key: str, entry: Entry) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO responses (key, body, etag, headers, fetched_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "body=excluded.body, etag=excluded.etag, "
                "headers=excluded.headers, "
                "fetched_at=excluded.fetched_at, expires_at=excluded.expires_at",
                (key, entry.body, entry.etag, json.dumps(dict(entry.headers)),
                 entry.fetched_at, entry.expires_at))
            self._db.commit()

    def touch(self, key: str, expires_at: float) -> None:
        """A 304 said the body is still good; just push the expiry out.

        fetched_at moves too: "as of 14:32" in the UI should mean "we
        confirmed this at 14:32", and a 304 is a confirmation.
        """
        with self._lock:
            self._db.execute(
                "UPDATE responses SET expires_at = ?, fetched_at = ? WHERE key = ?",
                (expires_at, self._clock(), key))
            self._db.commit()

    def status(self, key: str) -> Freshness | None:
        with self._lock:
            row = self._db.execute(
                "SELECT fetched_at, expires_at FROM responses WHERE key = ?",
                (key,)).fetchone()
        return Freshness(row[0], row[1]) if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()
