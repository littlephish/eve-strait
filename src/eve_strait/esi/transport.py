"""The single path every ESI request takes: governed, cached, logged.

client.py talks to this and nothing else. Auth is deliberately not handled
here -- callers pass their own Authorization header -- so this module stays
independent of the token store and easy to test.
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from .. import config
from .auth import RateLimited
from .httpcache import Entry, HttpCache, cache_key, parse_expires
from .ratelimit import MAX_INTERACTIVE_WAIT, RateLimitGovernor, route_key

log = logging.getLogger(__name__)

NEVER = 0.0                  # do not cache this route at all
PERMANENT = float("inf")     # cache forever; the resource cannot change

# Overrides on top of ESI's own `expires` header. Only two reasons to appear
# here: the resource is genuinely immutable (longer), or the data is live by
# definition (never). Everything absent from this table uses `expires`.
#
# /universe/names/ is absent on purpose despite being immutable: it is a POST
# whose input is a JSON body, and cache_key hashes params, not bodies. Caching
# it would need body-aware keying, which is follow-up work; it already batches
# 1000 IDs per call, so it is not the burst that hurt us.
CACHE_POLICY: dict[str, float] = {
    "/universe/stations/{id}/": PERMANENT,
    "/universe/structures/{id}/": 7 * 24 * 3600,
    "/characters/{id}/location/": NEVER,
    "/characters/{id}/ship/": NEVER,
    "/characters/{id}/online/": NEVER,
}


class Response:
    """Uniform result whether it came from sqlite or the wire."""

    def __init__(self, body: bytes, headers, status_code: int,
                 from_cache: bool, fetched_at: float, expires_at: float):
        self.content = body
        self.headers = headers
        self.status_code = status_code
        self.from_cache = from_cache
        self.fetched_at = fetched_at
        self.expires_at = expires_at

    def json(self):
        import json
        return json.loads(self.content) if self.content else None


class EsiTransport:
    def __init__(self, session=None, cache=None, governor=None,
                 clock=time.time, sleeper=time.sleep):
        self.session = session or requests.Session()
        self.cache = cache or HttpCache(config.CACHE_DIR / "esi_cache.sqlite")
        self.governor = governor or RateLimitGovernor(clock=clock)
        self._clock = clock
        self._sleep = sleeper

    # -- policy -------------------------------------------------------------
    def _expiry_for(self, path: str, headers, now: float) -> float:
        override = CACHE_POLICY.get(route_key(path))
        if override is not None:
            return now + override if override != PERMANENT else PERMANENT
        return parse_expires(headers, now) or now

    def _cacheable(self, path: str) -> bool:
        return CACHE_POLICY.get(route_key(path), None) != NEVER

    # -- public -------------------------------------------------------------
    def cache_status(self, path, params=None, character_id=None):
        return self.cache.status(
            cache_key("GET", f"{config.ESI_BASE}{path}", params, character_id))

    def get(self, path, *, params=None, character_id=None, headers=None,
            priority="interactive", force=False, timeout=30) -> Response:
        url = f"{config.ESI_BASE}{path}"
        cacheable = self._cacheable(path)
        key = cache_key("GET", url, params, character_id)
        entry = self.cache.get(key) if cacheable else None
        now = self._clock()

        if entry and not force and entry.expires_at > now:
            return self._from_cache(entry)

        decision = self.governor.check(path, character_id, priority)
        if decision.action == "decline":
            if entry:
                log.debug("ESI budget low (%s); serving stale %s",
                          decision.reason, path)
                return self._from_cache(entry)
            raise RateLimited(
                "EVE's rate limit is nearly spent and there is no cached copy "
                "of this data yet. Try again shortly.")
        if decision.action == "wait":
            if decision.seconds > MAX_INTERACTIVE_WAIT:
                if entry:
                    return self._from_cache(entry)
                raise RateLimited(
                    f"EVE is rate-limiting this application for another "
                    f"{int(decision.seconds)} seconds.")
            self._sleep(decision.seconds)

        req_headers = dict(headers or {})
        if entry and entry.etag and not force:
            req_headers["If-None-Match"] = entry.etag

        resp = self.session.request("GET", url, headers=req_headers,
                                    params=params, timeout=timeout)
        self._observe(path, character_id, resp)

        if resp.status_code == 429:
            retry_after = _retry_after(resp.headers)
            self.governor.park(path, character_id, retry_after)
            if retry_after <= MAX_INTERACTIVE_WAIT and priority == "interactive":
                self._sleep(retry_after)
                resp = self.session.request("GET", url, headers=req_headers,
                                            params=params, timeout=timeout)
                self._observe(path, character_id, resp)
            if resp.status_code == 429:
                raise RateLimited(
                    "EVE is rate-limiting requests right now (HTTP 429). "
                    f"Retry in about {int(retry_after)} seconds.")

        now = self._clock()
        if resp.status_code == 304 and entry:
            expires_at = self._expiry_for(path, resp.headers, now)
            self.cache.touch(key, expires_at)
            # The stored headers, not the 304's: a 304 body is empty and its
            # headers omit X-Pages, which assets() needs to walk pagination.
            return Response(entry.body, entry.headers, 200, True, now, expires_at)

        resp.raise_for_status()
        expires_at = self._expiry_for(path, resp.headers, now)
        if cacheable:
            self.cache.put(key, Entry(body=resp.content,
                                      etag=resp.headers.get("etag", ""),
                                      fetched_at=now, expires_at=expires_at,
                                      headers=dict(resp.headers)))
        return Response(resp.content, resp.headers, resp.status_code,
                        False, now, expires_at)

    def _from_cache(self, entry) -> Response:
        return Response(entry.body, entry.headers, 200, True,
                        entry.fetched_at, entry.expires_at)

    def post(self, path, *, params=None, json=None, character_id=None,
             headers=None, timeout=30):
        """Never cached. Still governed: writes cost tokens too.

        Returns the raw requests.Response, because every POST caller here
        already handles it that way and none of them want caching.
        """
        url = f"{config.ESI_BASE}{path}"
        decision = self.governor.check(path, character_id, "interactive")
        if decision.action == "wait" and decision.seconds <= MAX_INTERACTIVE_WAIT:
            self._sleep(decision.seconds)
        resp = self.session.request("POST", url, headers=dict(headers or {}),
                                    params=params, json=json, timeout=timeout)
        self._observe(path, character_id, resp)
        return resp

    # -- internals ----------------------------------------------------------
    def _observe(self, path, character_id, resp) -> None:
        self.governor.observe(path, character_id, resp.headers)
        self.governor.observe_errors(resp.headers)
        limit = resp.headers.get("X-Ratelimit-Limit")
        if limit:
            log.debug("ESI %s group=%s limit=%s remaining=%s", path,
                      resp.headers.get("X-Ratelimit-Group"), limit,
                      resp.headers.get("X-Ratelimit-Remaining"))


def _retry_after(headers) -> float:
    try:
        return float(headers.get("Retry-After", 60))
    except (TypeError, ValueError):
        return 60.0


_transport = None
_transport_lock = threading.Lock()


def get_transport() -> EsiTransport:
    global _transport
    with _transport_lock:
        if _transport is None:
            _transport = EsiTransport()
        return _transport
