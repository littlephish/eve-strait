"""Pochven is its own component of the travel graph.

All of this rides on one rule -- ``System.jumpable`` is False in Pochven,
because the region is cyno-jammed -- plus the fact that no stargate crosses
the border. The point of these tests is that those two facts alone produce
every correct border behaviour, with no special cases in the router:

    k-space -> Pochven      impossible (nothing may land there)
    Pochven -> Pochven      by stargate only (nothing may land there either)
    Pochven -> k-space      fine, by jump: the jammer stops a cyno being lit
                            *in* Pochven, not a drive being activated there
    k-space -> k-space      can never launder through the region

Built on a synthetic universe so this stays pure logic with no download, like
the rest of the suite. The shape is the one that actually bit before the rule
existed: a Pochven system exactly between two k-space systems that are further
apart than one jump, so a planner with no notion of Triglavian space takes the
shortcut. On the real map that is Maila -> Senda -> Oshaima, two jumps for a
carrier, and not flyable.
"""
import math

import pytest

from eve_strait.data import pochven as pdata
from eve_strait.data.ships import SHIPS_BY_NAME, Skills
from eve_strait.data.universe import POCHVEN_REGION_ID, System, Universe
from eve_strait.jump import router

KSPACE_REGION = 10_000_001


def make(sid, name, x, z=0.0, region=KSPACE_REGION):
    return System(id=sid, name=name, x=x, y=0.0, z=z, security=-0.5,
                  region_id=region, constellation_id=1)


@pytest.fixture
def uni():
    # Aaa --5ly-- Trig1 --2ly-- Trig2 --3ly-- Bbb   (Aaa..Bbb is 10 ly)
    # with a k-space detour Cee/Dee off-axis, every hop under 6 ly.
    systems = {
        1: make(1, "Aaa", 0.0),
        2: make(2, "Trig1", 5.0, region=POCHVEN_REGION_ID),
        3: make(3, "Trig2", 7.0, region=POCHVEN_REGION_ID),
        4: make(4, "Bbb", 10.0),
        5: make(5, "Cee", 3.5, z=-4.0),
        6: make(6, "Dee", 6.5, z=-4.0),
    }
    # The only stargates are Pochven's own internal pair. Nothing crosses the
    # border, exactly as in the real SDE.
    gates = {2: {3}, 3: {2}}
    return Universe(systems, gates=gates)


@pytest.fixture
def carrier():
    # 3 ly base x (1 + 0.20 x 5) = 6 ly.
    return SHIPS_BY_NAME["Thanatos"], Skills(jump_drive_calibration=5)


def plan(uni, carrier, a, b, minimize="jumps"):
    ship, skills = carrier
    return router.plan_multimodal(uni, ship, skills, uni.systems[a],
                                  uni.systems[b], minimize=minimize)


# -- the rule itself --------------------------------------------------------
def test_pochven_systems_are_identified(uni):
    assert uni.pochven_ids == frozenset({2, 3})
    assert uni.systems[2].pochven is True
    assert uni.systems[1].pochven is False


def test_pochven_is_never_a_jump_landing(uni):
    """Null-sec, so the security rule alone would have allowed it."""
    assert uni.systems[2].security < 0.5
    assert uni.systems[2].jumpable is False
    assert uni.systems[1].jumpable is True


def test_within_range_omits_pochven(uni):
    """The reach circle and 'systems in jump range' both ride on this."""
    reach = uni.within_range(uni.systems[1], 6.0)
    assert [s.name for s, _ in reach] == ["Cee"]
    # ...but it is still there when the caller asks for raw geometry, which
    # is what the Proximity-filament search needs.
    raw = uni.within_range(uni.systems[1], 6.0, jumpable_only=False)
    assert "Trig1" in [s.name for s, _ in raw]


# -- the four border directions ---------------------------------------------
def test_kspace_into_pochven_is_impossible(uni, carrier):
    assert plan(uni, carrier, 1, 2) is None


def test_pochven_out_to_kspace_is_a_legal_jump(uni, carrier):
    """A capital stranded inside can still jump out to a cyno lit outside."""
    res = plan(uni, carrier, 2, 1)
    assert res is not None
    systems, modes = res
    assert [s.name for s in systems] == ["Trig1", "Aaa"]
    assert modes == ["jump"]


def test_inside_pochven_routes_by_stargate_only(uni, carrier):
    res = plan(uni, carrier, 2, 3)
    assert res is not None
    systems, modes = res
    assert [s.name for s in systems] == ["Trig1", "Trig2"]
    # 2 ly apart and well within jump range, but no cyno can be lit at the
    # far end, so the only way across is the gate.
    assert modes == ["gate"]


def test_kspace_route_never_launders_through_pochven(uni, carrier):
    res = plan(uni, carrier, 1, 4, minimize="only_jumps")
    assert res is not None
    systems, modes = res
    names = [s.name for s in systems]
    assert "Trig1" not in names and "Trig2" not in names
    assert names == ["Aaa", "Cee", "Dee", "Bbb"]


def test_no_route_when_pochven_is_the_only_shortcut(carrier):
    """Without the k-space detour the honest answer is 'no route'."""
    uni = Universe({
        1: make(1, "Aaa", 0.0),
        2: make(2, "Trig", 5.0, region=POCHVEN_REGION_ID),
        3: make(3, "Bbb", 10.0),
    })
    assert plan(uni, carrier, 1, 3, minimize="only_jumps") is None


# -- what the pilot is told -------------------------------------------------
def test_manual_jump_leg_into_pochven_names_the_jammer(uni, carrier):
    """simulate() must not blame high-sec for a cyno-jammed region."""
    ship, skills = carrier
    plan_ = router.simulate(ship, skills, [uni.systems[1], uni.systems[2]],
                            ["jump"])
    leg = plan_.legs[0]
    assert leg.in_range is False
    assert "Pochven" in leg.reason and "hi-sec" not in leg.reason


# -- the inset is a translation, and must stay one --------------------------
def test_inset_offset_preserves_every_distance():
    """The whole reason the inset is drawn this way.

    On this map position *is* distance and distance *is* jump range, under a
    scale bar that covers the whole scene. So the inset may move Pochven and
    may do nothing else: rescale or re-lay-it-out and the scale bar starts
    telling lies about the box. An earlier version laid the systems out by
    their gate graph, which drew a tidy ring that meant nothing in ly.
    """
    kspace = (-53.8, -50.0, 35.6, 51.2)
    pochven = (-25.2, -17.4, -0.7, 10.6)
    dx, dy = pdata.inset_offset(kspace, pochven)

    pts = [(-25.2, -17.4), (-0.7, 10.6), (-12.0, 3.3), (-20.1, -8.8)]
    moved = [(x + dx, y + dy) for x, y in pts]
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            before = math.dist(pts[i], pts[j])
            after = math.dist(moved[i], moved[j])
            assert after == pytest.approx(before), "inset must not rescale"


def test_inset_sits_clear_of_new_eden():
    kspace = (-53.8, -50.0, 35.6, 51.2)
    pochven = (-25.2, -17.4, -0.7, 10.6)
    dx, dy = pdata.inset_offset(kspace, pochven)
    assert pochven[0] + dx == pytest.approx(35.6 + pdata.INSET_GAP_LY)
    # Vertically centred on New Eden.
    box_mid = (pochven[1] + dy + pochven[3] + dy) / 2
    assert box_mid == pytest.approx((-50.0 + 51.2) / 2)


def test_clade_and_home_tables_agree():
    assert len(pdata.CLADES) == 3
    assert set(pdata.HOME_SYSTEMS) == set(pdata.CLADES.values())


def test_region_labels_carry_their_id():
    """The map needs the id to place Pochven's label without name matching."""
    uni = Universe({1: make(1, "Aaa", 0.0)},
                   regions=[("Pochven", 1.0, 2.0, POCHVEN_REGION_ID)])
    assert uni.regions[0][3] == POCHVEN_REGION_ID
