"""Merging the same hole as seen by two sources.

EVE-Scout and a Wanderer map routinely describe the same connection. The
old merge kept whichever record had the larger mass limit and threw the
other away wholesale -- so a hole your own map had marked end-of-life and
mass-critical sailed through both safety filters, because the winning
record simply had no such fields.
"""
import datetime as dt

from eve_strait.esi import evescout


def iso(minutes_ago):
    return (dt.datetime.now(dt.UTC)
            - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scout(**over):
    base = {"via": "Turnur", "hops": 1, "size": "capital",
            "wh_types": ["N944"], "max_t": 2_000_000,
            "sigs": {1: "YWN-072"}, "hours": 16,
            "updated_at": iso(370), "updated_at_all": [iso(370)]}
    base.update(over)
    return base


def _wanderer(**over):
    base = {"via": "Wanderer", "hops": 1, "size": "unknown",
            "wh_types": ["K162"], "max_t": 62_000, "sigs": {}, "hours": None,
            "life": "end of life", "mass": "critical"}
    base.update(over)
    return base


def test_mass_limit_takes_the_better_informed_value():
    """Wanderer's 62,000 is a 'type unknown' fallback, not a measurement."""
    got = evescout.merge_edge(_scout(), _wanderer())
    assert got["max_t"] == 2_000_000


def test_worst_known_life_and_mass_survive():
    """The whole point: a warning must not be lost to the other record."""
    got = evescout.merge_edge(_scout(), _wanderer())
    assert got["life"] == "end of life"
    assert got["mass"] == "critical"


def test_merge_is_order_independent():
    a = evescout.merge_edge(_scout(), _wanderer())
    b = evescout.merge_edge(_wanderer(), _scout())
    for key in ("max_t", "life", "mass", "hops"):
        assert a[key] == b[key], key


def test_freshest_timestamps_win():
    fresh = _wanderer(updated_at=iso(5), updated_at_all=[iso(5)])
    got = evescout.merge_edge(_scout(), fresh)
    age = evescout.edge_age_minutes(got)
    assert age is not None and age < 30, age


def test_signatures_are_combined():
    got = evescout.merge_edge(_scout(), _wanderer(sigs={2: "ABC-111"}))
    assert got["sigs"] == {1: "YWN-072", 2: "ABC-111"}


def test_fewer_hops_wins():
    got = evescout.merge_edge(_scout(hops=3), _wanderer(hops=1))
    assert got["hops"] == 1


def test_both_sources_are_recorded():
    got = evescout.merge_edge(_scout(), _wanderer())
    assert set(got["sources"]) == {"Turnur", "Wanderer"}


def test_merging_with_nothing_returns_the_other():
    assert evescout.merge_edge(None, _scout())["max_t"] == 2_000_000
    assert evescout.merge_edge(_scout(), None)["max_t"] == 2_000_000


def test_unknown_status_never_overrides_a_known_one():
    """Absent is not 'fine'; it is 'no information'."""
    got = evescout.merge_edge(_scout(), _wanderer(life=None, mass=None))
    assert got.get("life") is None and got.get("mass") is None
    got2 = evescout.merge_edge(_scout(), _wanderer(life="fresh", mass="reduced"))
    assert got2["life"] == "fresh" and got2["mass"] == "reduced"
