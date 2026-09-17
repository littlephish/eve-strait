"""A wormhole leg is unflyable without its signatures.

You cannot warp to a wormhole you cannot find, so the signature at each end
is not decoration -- it is the leg. A Thera crossing needs three of them:
the one in the system you start in, the one *inside Thera* that leads
onward, and the one at the far end so you know you arrived at the right
hole and can find it again coming back.

graph() kept only the two k-space signatures, so the middle step -- the one
you need while standing in Thera -- was silently dropped.
"""
from eve_strait.esi import evescout


def _conn(hub, system_id, hub_sig, far_sig, **over):
    base = {"hub": hub, "system_id": system_id, "hub_sig": hub_sig,
            "far_sig": far_sig, "size": "large", "wh_type": "B449",
            "hours": 16, "updated_at": None}
    base.update(over)
    return base


def test_turnur_edge_keeps_both_ends():
    edges = evescout.graph([_conn("Turnur", 100, "AAA-111", "BBB-222")],
                           turnur_id=9)
    (info,) = edges.values()
    assert info["sigs"] == {9: "AAA-111", 100: "BBB-222"}


def test_thera_crossing_keeps_the_hub_side_signatures():
    """The whole point: standing in Thera, which sig leads onward?"""
    edges = evescout.graph([_conn("Thera", 100, "AAA-111", "BBB-222"),
                            _conn("Thera", 200, "CCC-333", "DDD-444")],
                           turnur_id=9)
    (info,) = edges.values()
    assert info["sigs"] == {100: "BBB-222", 200: "DDD-444"}
    # In Thera, AAA-111 leads to 100 and CCC-333 leads to 200.
    assert info["via_sigs"] == {100: "AAA-111", 200: "CCC-333"}


def test_crossing_lists_the_steps_in_flying_order():
    edges = evescout.graph([_conn("Thera", 100, "AAA-111", "BBB-222"),
                            _conn("Thera", 200, "CCC-333", "DDD-444")],
                           turnur_id=9)
    (info,) = edges.values()
    assert evescout.crossing(info, 100, 200) == [
        (100, "BBB-222"), ("Thera", "CCC-333")]
    # and the reverse direction uses the other pair
    assert evescout.crossing(info, 200, 100) == [
        (200, "DDD-444"), ("Thera", "AAA-111")]


def test_crossing_of_a_single_hop_is_one_step():
    edges = evescout.graph([_conn("Turnur", 100, "AAA-111", "BBB-222")],
                           turnur_id=9)
    (info,) = edges.values()
    assert evescout.crossing(info, 9, 100) == [(9, "AAA-111")]
    assert evescout.crossing(info, 100, 9) == [(100, "BBB-222")]


def test_arrival_signature_is_the_far_end():
    edges = evescout.graph([_conn("Turnur", 100, "AAA-111", "BBB-222")],
                           turnur_id=9)
    (info,) = edges.values()
    assert evescout.arrival_sig(info, 100) == "BBB-222"


def test_missing_signatures_degrade_to_none():
    """Wanderer edges carry none; the leg is still routable."""
    info = {"via": "Wanderer", "hops": 1, "sigs": {}}
    assert evescout.crossing(info, 1, 2) == [(1, None)]
    assert evescout.arrival_sig(info, 2) is None


def test_merge_keeps_hub_signatures():
    a = {"via": "Thera", "hops": 2, "sigs": {1: "AAA-111"},
         "via_sigs": {1: "HUB-001"}, "max_t": 1000}
    b = {"via": "Wanderer", "hops": 2, "sigs": {2: "BBB-222"}, "max_t": 2000}
    got = evescout.merge_edge(a, b)
    assert got["sigs"] == {1: "AAA-111", 2: "BBB-222"}
    assert got["via_sigs"] == {1: "HUB-001"}
