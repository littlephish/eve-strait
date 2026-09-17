"""Ansiblex edges obey the Cradle of War hull rule.

Synthetic universe, following tests/test_pochven.py: two systems 20 ly apart
with no stargate between them and an Ansiblex link. That is further than any
hull can jump, so the bridge is the only way through -- which makes "is there
a route at all" a clean proxy for "was the bridge edge offered".
"""
import pytest

from eve_strait.data.ships import SHIPS_BY_NAME, Skills
from eve_strait.data.universe import System, Universe
from eve_strait.jump import router

REGION = 10_000_001


def make(sid, name, x):
    return System(id=sid, name=name, x=x, y=0.0, z=0.0, security=-0.5,
                  region_id=REGION, constellation_id=1)


@pytest.fixture
def uni():
    systems = {1: make(1, "Aaa", 0.0), 2: make(2, "Bbb", 20.0)}
    u = Universe(systems, gates={})
    u.set_bridges([["Aaa", "Bbb"]])
    return u


def plan(uni, ship_name, jdc=5):
    ship = SHIPS_BY_NAME[ship_name]
    return router.plan_multimodal(uni, ship, Skills(jump_drive_calibration=jdc),
                                  uni.systems[1], uni.systems[2])


def test_jump_freighter_may_use_the_bridge(uni):
    result = plan(uni, "Rhea")
    assert result is not None
    _systems, modes = result
    assert modes == ["bridge"]


def test_rorqual_may_use_the_bridge(uni):
    assert plan(uni, "Rorqual") is not None


@pytest.mark.parametrize("name", ["Thanatos", "Revelation", "Apostle", "Avatar"])
def test_capitals_are_refused_the_bridge(uni, name):
    """20 ly with no stargate: without the bridge there is no route at all."""
    assert plan(uni, name) is None


def _owned_uni(alliance_id):
    systems = {1: make(1, "Aaa", 0.0), 2: make(2, "Bbb", 20.0)}
    u = Universe(systems, gates={})
    u.set_bridges([{"a": "Aaa", "b": "Bbb", "alliance_id": alliance_id,
                    "source": "esi"}])
    return u


def _plan(uni, my_alliance_id):
    return router.plan_multimodal(
        uni, SHIPS_BY_NAME["Rhea"], Skills(jump_drive_calibration=5),
        uni.systems[1], uni.systems[2], my_alliance_id=my_alliance_id)


def test_own_alliance_gate_is_usable():
    assert _plan(_owned_uni(99), 99) is not None


def test_other_alliance_gate_is_refused():
    assert _plan(_owned_uni(99), 1234) is None


def test_unknown_owner_is_trusted():
    """Hand-typed and legacy gates must keep working on upgrade."""
    assert _plan(_owned_uni(None), 1234) is not None


def test_unknown_pilot_alliance_permits_everything():
    """Not logged in: we cannot judge, so we do not block."""
    assert _plan(_owned_uni(99), None) is not None
