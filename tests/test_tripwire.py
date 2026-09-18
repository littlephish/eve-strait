"""Tripwire chains become routable k-space edges.

Tripwire is the best-informed of the three sources: it carries signatures at
both ends, life and mass status, and a modification time. The parsing has to
keep all of that, because those are exactly the fields the leg table and the
safety filters read.

The payload shape here is Tripwire's real one -- two tables, `wormholes`
referencing `signatures` by id at each end -- taken from Short Circuit's
committed example.
"""
import datetime as dt

from eve_strait.esi import tripwire

JITA, AMARR, DODIXIE = 30000142, 30002187, 30002659
J_A, J_B = 31000001, 31000002          # J-space: never in Universe.systems
SYSTEMS = {JITA: object(), AMARR: object(), DODIXIE: object()}


def _stamp(minutes_ago):
    t = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _payload(links):
    """Build a Tripwire-shaped response from (sysA, sigA, sysB, sigB, **kw)."""
    sigs, whs = {}, {}
    for i, link in enumerate(links):
        a_id, b_id = f"a{i}", f"b{i}"
        sigs[a_id] = {"id": a_id, "signatureID": link["a_sig"],
                      "systemID": str(link["a_sys"]), "type": "wormhole",
                      "modifiedTime": link.get("modified", _stamp(5))}
        sigs[b_id] = {"id": b_id, "signatureID": link["b_sig"],
                      "systemID": str(link["b_sys"]), "type": "wormhole",
                      "modifiedTime": link.get("modified", _stamp(5))}
        whs[str(i)] = {"id": str(i), "initialID": a_id, "secondaryID": b_id,
                       "type": link.get("type", ""),
                       "life": link.get("life", "stable"),
                       "mass": link.get("mass", "stable")}
    return {"signatures": sigs, "wormholes": whs}


def test_direct_kspace_link_becomes_one_edge():
    data = _payload([{"a_sys": JITA, "a_sig": "ABC-123",
                      "b_sys": AMARR, "b_sig": "XYZ-789", "type": "B449"}])
    edges = tripwire.edges(data, SYSTEMS)
    assert list(edges) == [(JITA, AMARR)]
    info = edges[(JITA, AMARR)]
    assert info["hops"] == 1
    assert info["max_t"] == 1_000_000          # B449, from the type table
    assert info["sigs"] == {JITA: "ABC-123", AMARR: "XYZ-789"}


def test_jspace_chain_is_collapsed_into_one_edge():
    """k -> J -> J -> k is the interesting case and must survive."""
    data = _payload([
        {"a_sys": JITA, "a_sig": "AAA-111", "b_sys": J_A, "b_sig": "BBB-222"},
        {"a_sys": J_A, "a_sig": "CCC-333", "b_sys": J_B, "b_sig": "DDD-444"},
        {"a_sys": J_B, "a_sig": "EEE-555", "b_sys": AMARR, "b_sig": "FFF-666"},
    ])
    edges = tripwire.edges(data, SYSTEMS)
    assert (JITA, AMARR) in edges
    assert edges[(JITA, AMARR)]["hops"] == 3


def test_the_signature_kept_is_the_one_where_you_stand():
    data = _payload([
        {"a_sys": JITA, "a_sig": "AAA-111", "b_sys": J_A, "b_sig": "BBB-222"},
        {"a_sys": J_A, "a_sig": "CCC-333", "b_sys": AMARR, "b_sig": "FFF-666"},
    ])
    info = tripwire.edges(data, SYSTEMS)[(JITA, AMARR)]
    # Leaving Jita you warp to AAA-111; arriving in Amarr you are at FFF-666.
    assert info["sigs"][JITA] == "AAA-111"
    assert info["sigs"][AMARR] == "FFF-666"


def test_worst_status_along_the_chain_wins():
    """A route is only as safe as its shakiest hole."""
    data = _payload([
        {"a_sys": JITA, "a_sig": "A", "b_sys": J_A, "b_sig": "B",
         "life": "stable", "mass": "stable"},
        {"a_sys": J_A, "a_sig": "C", "b_sys": AMARR, "b_sig": "D",
         "life": "critical", "mass": "destab"},
    ])
    info = tripwire.edges(data, SYSTEMS)[(JITA, AMARR)]
    assert info["life"] == "end of life"
    assert info["mass"] == "reduced"


def test_smallest_hole_limits_the_whole_chain():
    data = _payload([
        {"a_sys": JITA, "a_sig": "A", "b_sys": J_A, "b_sig": "B",
         "type": "B449"},                       # 1,000,000 t
        {"a_sys": J_A, "a_sig": "C", "b_sys": AMARR, "b_sig": "D",
         "type": "F353"},                       # 62,000 t
    ])
    assert tripwire.edges(data, SYSTEMS)[(JITA, AMARR)]["max_t"] == 62_000


def test_chain_is_as_old_as_its_stalest_hop():
    data = _payload([
        {"a_sys": JITA, "a_sig": "A", "b_sys": J_A, "b_sig": "B",
         "modified": _stamp(5)},
        {"a_sys": J_A, "a_sig": "C", "b_sys": AMARR, "b_sig": "D",
         "modified": _stamp(200)},
    ])
    from eve_strait.esi import evescout
    info = tripwire.edges(data, SYSTEMS)[(JITA, AMARR)]
    age = evescout.edge_age_minutes(info)
    assert age is not None and 195 <= age <= 205


def test_unfilled_signature_reads_as_absent_not_as_an_id():
    """Tripwire stores '???' for a signature nobody has typed in yet."""
    data = _payload([{"a_sys": JITA, "a_sig": "???",
                      "b_sys": AMARR, "b_sig": None}])
    info = tripwire.edges(data, SYSTEMS)[(JITA, AMARR)]
    assert info["sigs"] == {}


def test_unknown_type_is_treated_conservatively():
    """K162 is the generic exit signature and says nothing about size."""
    data = _payload([{"a_sys": JITA, "a_sig": "A", "b_sys": AMARR,
                      "b_sig": "B", "type": "K162"}])
    assert tripwire.edges(data, SYSTEMS)[(JITA, AMARR)]["max_t"] == 62_000


def test_dangling_and_bogus_entries_are_dropped_not_fatal():
    data = _payload([{"a_sys": JITA, "a_sig": "A", "b_sys": AMARR, "b_sig": "B"}])
    data["wormholes"]["99"] = {"id": "99", "initialID": "nope",
                               "secondaryID": "also-nope"}
    data["signatures"]["bad"] = {"id": "bad", "signatureID": "X",
                                 "systemID": "1", "type": "wormhole"}
    edges = tripwire.edges(data, SYSTEMS)
    assert list(edges) == [(JITA, AMARR)]


def test_empty_payload_is_empty():
    assert tripwire.edges({}, SYSTEMS) == {}
    assert tripwire.edges(None, SYSTEMS) == {}


# -- against a real-shaped payload ------------------------------------------
# The synthetic payloads above are built by this file, so they can only prove
# the parser agrees with itself. This one is Tripwire's actual response shape.
def _fixture():
    import json
    from pathlib import Path
    path = Path(__file__).parent / "data" / "tripwire_chain.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixture_is_the_real_shape():
    data = _fixture()
    assert set(data) >= {"signatures", "wormholes", "sync"}
    sig = next(iter(data["signatures"].values()))
    assert {"signatureID", "systemID", "modifiedTime"} <= set(sig)
    wh = next(iter(data["wormholes"].values()))
    assert {"initialID", "secondaryID", "life", "mass"} <= set(wh)


def test_fixture_carries_no_credentials():
    """It was scrubbed on the way in; keep it that way."""
    import json
    assert _fixture()["esi"] == {}
    blob = json.dumps(_fixture())
    for bad in ("accessToken", "refreshToken", "eyJhbGci"):
        assert bad not in blob


def test_fixture_parses_into_edges():
    data = _fixture()
    # The fixture's systems, minus its deliberately bogus "1".
    systems = {int(s["systemID"]): object()
               for s in data["signatures"].values()
               if str(s["systemID"]).isdigit() and int(s["systemID"]) > 30000000}
    edges = tripwire.edges(data, systems)
    assert edges, "a chain of seven connections should yield edges"
    for (a, b), info in edges.items():
        assert a in systems and b in systems
        assert a < b                       # keys are the sorted pair
        assert info["via"] == "Tripwire"
        assert info["max_t"] > 0
        assert info["hops"] >= 1


def test_bogus_system_id_in_the_fixture_is_dropped():
    """The fixture contains systemID '1', which is not a solar system."""
    data = _fixture()
    systems = {int(s["signatureID"] or 0): object() for s in ()}  # empty
    systems = {30002659: object(), 30000222: object()}
    edges = tripwire.edges(data, systems)
    for pair in edges:
        assert 1 not in pair
