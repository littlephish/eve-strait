import pytest

from eve_strait.esi.ratelimit import Limit, parse_limit, route_key


@pytest.mark.parametrize("raw,expected", [
    ("150/15m", Limit(150, 900)),
    ("1000/15m", Limit(1000, 900)),
    ("1000/1h", Limit(1000, 3600)),
    ("60/30s", Limit(60, 30)),
])
def test_parse_limit_understands_ccp_format(raw, expected):
    assert parse_limit(raw) == expected


@pytest.mark.parametrize("raw", ["", "nonsense", "150", "150/15x", "abc/15m", None])
def test_parse_limit_returns_none_on_junk(raw):
    assert parse_limit(raw) is None


def test_route_key_collapses_numeric_ids():
    assert route_key("/characters/12345/assets/") == "/characters/{id}/assets/"
    assert route_key("/universe/stations/60003760/") == "/universe/stations/{id}/"


def test_route_key_leaves_static_paths_alone():
    assert route_key("/sovereignty/map/") == "/sovereignty/map/"
