"""The footer should say what a route is made of.

RoutePlan already counts every mode; the footer omitted wormholes, so a
route that leaned on a scouted hole looked like a plain gate route.
"""
from eve_strait.jump.router import Leg, RoutePlan, compose


def _leg(mode):
    return Leg(src=None, dst=None, mode=mode, distance_ly=1.0, fuel=0,
               in_range=True, cooldown_min=0.0, fatigue_after_min=0.0,
               wait_before_min=0.0, t_depart_min=0.0)


def _plan(*modes):
    return RoutePlan(legs=[_leg(m) for m in modes])


def test_empty_plan_is_empty():
    assert compose(RoutePlan()) == ""


def test_counts_every_mode_present():
    assert compose(_plan("jump", "jump", "gate")) == "2 jumps, 1 gate"


def test_omits_modes_that_are_absent():
    """A pure gate route should not read '0 jumps, 0 wormholes'."""
    assert compose(_plan("gate", "gate")) == "2 gates"


def test_wormholes_and_ansiblex_are_named():
    out = compose(_plan("jump", "gate", "bridge", "hole"))
    assert out == "1 jump, 1 gate, 1 ansiblex, 1 wormhole"


def test_singular_and_plural():
    assert compose(_plan("hole")) == "1 wormhole"
    assert compose(_plan("hole", "hole")) == "2 wormholes"
