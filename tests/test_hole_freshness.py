"""How old is the scan behind this wormhole leg?

A wormhole route is only as good as the last time somebody looked at the
hole. Remaining-life hours say how long it should last; the scan age says
how much to trust that number, and they are different questions.
"""
from datetime import datetime, timedelta, timezone

from eve_strait.esi import evescout


def _iso(minutes_ago):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_unknown_when_absent():
    """Absent must read as unknown, never as fresh."""
    assert evescout.edge_age_minutes({}) is None
    assert evescout.edge_age_minutes({"updated_at": None}) is None


def test_age_in_minutes():
    got = evescout.edge_age_minutes({"updated_at": _iso(30)})
    assert got is not None and 29 <= got <= 31


def test_collapsed_edge_takes_the_oldest_end():
    """A Thera crossing is two holes; it is only as fresh as the staler one."""
    info = {"updated_at": _iso(10), "updated_at_all": [_iso(10), _iso(90)]}
    got = evescout.edge_age_minutes(info)
    assert got is not None and 89 <= got <= 91


def test_graph_carries_updated_at():
    conns = [
        {"hub": "Turnur", "system_id": 30000001, "hub_sig": "AAA-111",
         "far_sig": "BBB-222", "size": "large", "wh_type": "B449",
         "hours": 16, "updated_at": _iso(5)},
    ]
    edges = evescout.graph(conns, turnur_id=30002718)
    (info,) = edges.values()
    assert info.get("updated_at") is not None
