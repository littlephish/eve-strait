import pytest

from eve_strait.esi.httpcache import (Entry, HttpCache, cache_key,
                                      parse_expires)


@pytest.fixture
def cache(tmp_path):
    c = HttpCache(tmp_path / "test.sqlite")
    yield c
    c.close()


def entry(body=b'{"ok":true}', etag="W/\"abc\"", fetched=1000.0, expires=4600.0,
          headers=None):
    return Entry(body=body, etag=etag, fetched_at=fetched, expires_at=expires,
                 headers=headers if headers is not None else {"X-Pages": "3"})


def test_missing_key_returns_none(cache):
    assert cache.get("nope") is None


def test_roundtrip_preserves_body_and_etag(cache):
    cache.put("k", entry())
    got = cache.get("k")
    assert got.body == b'{"ok":true}'
    assert got.etag == 'W/"abc"'
    assert got.expires_at == 4600.0


def test_roundtrip_preserves_headers(cache):
    # X-Pages lives here. Losing it truncates paginated results on a cache
    # hit, which looks like missing assets rather than like a cache bug.
    cache.put("k", entry(headers={"X-Pages": "3", "last-modified": "yesterday"}))
    assert cache.get("k").headers["X-Pages"] == "3"


def test_put_overwrites_an_existing_key(cache):
    cache.put("k", entry(body=b"old"))
    cache.put("k", entry(body=b"new"))
    assert cache.get("k").body == b"new"


def test_expired_entries_are_still_returned(cache):
    # The transport needs the stale body: it carries the ETag for
    # revalidation and it is what a declined background call serves.
    cache.put("k", entry(expires=1.0))
    assert cache.get("k") is not None


def test_touch_extends_expiry_without_changing_the_body(cache):
    cache.put("k", entry())
    cache.touch("k", 9999.0)
    got = cache.get("k")
    assert got.expires_at == 9999.0
    assert got.body == b'{"ok":true}'


def test_status_reports_freshness_without_the_body(cache):
    cache.put("k", entry())
    st = cache.status("k")
    assert (st.fetched_at, st.expires_at) == (1000.0, 4600.0)


def test_status_of_unknown_key_is_none(cache):
    assert cache.status("nope") is None


def test_cache_survives_reopening(tmp_path):
    path = tmp_path / "p.sqlite"
    c1 = HttpCache(path)
    c1.put("k", entry())
    c1.close()
    c2 = HttpCache(path)
    assert c2.get("k").body == b'{"ok":true}'
    c2.close()


def test_keys_are_isolated_per_character():
    a = cache_key("GET", "https://esi/x/", {"page": 1}, 111)
    b = cache_key("GET", "https://esi/x/", {"page": 1}, 222)
    assert a != b


def test_key_ignores_param_ordering():
    a = cache_key("GET", "https://esi/x/", {"page": 1, "b": 2}, 111)
    b = cache_key("GET", "https://esi/x/", {"b": 2, "page": 1}, 111)
    assert a == b


def test_parse_expires_reads_an_http_date():
    headers = {"expires": "Wed, 19 Aug 2026 12:00:00 GMT"}
    assert parse_expires(headers, now=0.0) == pytest.approx(1787140800.0)


def test_parse_expires_returns_none_without_the_header():
    assert parse_expires({}, now=0.0) is None


def test_parse_expires_ignores_junk():
    assert parse_expires({"expires": "0"}, now=0.0) is None
