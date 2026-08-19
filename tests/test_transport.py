import pytest

from eve_strait.esi.auth import RateLimited
from eve_strait.esi.httpcache import HttpCache
from eve_strait.esi.ratelimit import RateLimitGovernor
from eve_strait.esi.transport import EsiTransport


class FakeResponse:
    def __init__(self, status_code=200, body=b'{"v":1}', headers=None):
        self.status_code = status_code
        self.content = body
        self.headers = headers or {}

    def json(self):
        import json
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """Records requests and replays a scripted list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None,
                timeout=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "params": params,
                           "json": json})
        return self.responses.pop(0)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def make(tmp_path):
    def _make(responses, clock=None):
        clock = clock or FakeClock()
        slept = []
        t = EsiTransport(
            session=FakeSession(responses),
            cache=HttpCache(tmp_path / "t.sqlite", clock=clock),
            governor=RateLimitGovernor(clock=clock),
            clock=clock,
            sleeper=slept.append,
        )
        return t, t.session, slept, clock
    return _make


def fresh_headers(expires="Wed, 19 Aug 2036 12:00:00 GMT"):
    return {"expires": expires, "etag": 'W/"v1"',
            "X-Ratelimit-Group": "assets",
            "X-Ratelimit-Limit": "1000/15m",
            "X-Ratelimit-Remaining": "900"}


def test_first_call_hits_the_network_and_caches(make):
    t, session, _, _ = make([FakeResponse(headers=fresh_headers())])
    r = t.get("/characters/1/assets/", character_id=1)
    assert r.json() == {"v": 1}
    assert r.from_cache is False
    assert len(session.calls) == 1


def test_second_call_within_expiry_never_touches_the_network(make):
    t, session, _, _ = make([FakeResponse(headers=fresh_headers())])
    t.get("/characters/1/assets/", character_id=1)
    r = t.get("/characters/1/assets/", character_id=1)
    assert r.from_cache is True
    assert r.json() == {"v": 1}
    assert len(session.calls) == 1          # still one: the button was free


def test_cached_response_keeps_x_pages(make):
    # assets() walks pagination using this header. A cache hit that dropped
    # it would silently truncate a multi-page asset list to page 1.
    headers = dict(fresh_headers(), **{"X-Pages": "4"})
    t, _, _, _ = make([FakeResponse(headers=headers)])
    t.get("/characters/1/assets/", character_id=1, params={"page": 1})
    r = t.get("/characters/1/assets/", character_id=1, params={"page": 1})
    assert r.from_cache is True
    assert r.headers["X-Pages"] == "4"


def test_force_bypasses_a_fresh_entry(make):
    t, session, _, _ = make([FakeResponse(headers=fresh_headers()),
                             FakeResponse(body=b'{"v":2}',
                                          headers=fresh_headers())])
    t.get("/characters/1/assets/", character_id=1)
    r = t.get("/characters/1/assets/", character_id=1, force=True)
    assert r.json() == {"v": 2}
    assert len(session.calls) == 2


def test_expired_entry_revalidates_with_if_none_match(make):
    clock = FakeClock()
    t, session, _, _ = make([FakeResponse(headers=fresh_headers()),
                             FakeResponse(status_code=304, body=b"",
                                          headers=fresh_headers())], clock=clock)
    t.get("/characters/1/assets/", character_id=1)
    clock.t += 10_000_000_000                 # well past the expires header
    r = t.get("/characters/1/assets/", character_id=1)
    assert session.calls[1]["headers"]["If-None-Match"] == 'W/"v1"'
    assert r.json() == {"v": 1}               # stored body, not the empty 304
    assert r.from_cache is True


def test_never_cached_routes_always_hit_the_network(make):
    t, session, _, _ = make([FakeResponse(headers=fresh_headers()),
                             FakeResponse(headers=fresh_headers())])
    t.get("/characters/1/location/", character_id=1)
    t.get("/characters/1/location/", character_id=1)
    assert len(session.calls) == 2


def test_immutable_routes_are_cached_past_their_expires(make):
    clock = FakeClock()
    t, session, _, _ = make([FakeResponse(headers=fresh_headers())], clock=clock)
    t.get("/universe/stations/60003760/")
    clock.t += 10_000_000_000
    r = t.get("/universe/stations/60003760/")
    assert r.from_cache is True
    assert len(session.calls) == 1            # this is the dockables 429 fix


def test_429_parks_then_retries_once(make):
    t, session, slept, _ = make([
        FakeResponse(status_code=429, headers={"Retry-After": "5",
                                               "X-Ratelimit-Group": "assets"}),
        FakeResponse(headers=fresh_headers()),
    ])
    r = t.get("/characters/1/assets/", character_id=1)
    assert r.json() == {"v": 1}
    assert slept == [5.0]
    assert len(session.calls) == 2


def test_429_twice_raises_rate_limited(make):
    t, _, _, _ = make([
        FakeResponse(status_code=429, headers={"Retry-After": "5"}),
        FakeResponse(status_code=429, headers={"Retry-After": "5"}),
    ])
    with pytest.raises(RateLimited):
        t.get("/characters/1/assets/", character_id=1)


def test_a_long_retry_after_is_raised_not_slept_through(make):
    t, _, slept, _ = make([FakeResponse(status_code=429,
                                        headers={"Retry-After": "900"})])
    with pytest.raises(RateLimited):
        t.get("/characters/1/assets/", character_id=1)
    assert slept == []                        # never freeze a button for 15 min


def test_declined_background_call_serves_the_stale_body(make):
    clock = FakeClock()
    low = dict(fresh_headers(), **{"X-Ratelimit-Remaining": "10"})   # 1%
    t, session, _, _ = make([FakeResponse(headers=low)], clock=clock)
    t.get("/characters/1/assets/", character_id=1)
    clock.t += 10_000_000_000
    r = t.get("/characters/1/assets/", character_id=1, priority="background")
    assert r.from_cache is True
    assert len(session.calls) == 1


def test_declined_background_call_without_a_cache_entry_raises(make):
    t, _, _, _ = make([])
    t.governor.park("/characters/1/assets/", 1, 60)
    with pytest.raises(RateLimited):
        t.get("/characters/1/assets/", character_id=1, priority="background")


def test_cache_status_reports_without_a_request(make):
    t, session, _, _ = make([FakeResponse(headers=fresh_headers())])
    t.get("/characters/1/assets/", character_id=1)
    st = t.cache_status("/characters/1/assets/", character_id=1)
    assert st.expires_at > st.fetched_at
    assert len(session.calls) == 1
