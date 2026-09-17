"""Contiguous gate hops collapse to one row.

A capital route can gate twenty times between two jumps; listing each one
buries the jumps, which are the legs that cost fuel and fatigue.
"""
from eve_strait.jump.router import Leg, collapse_gate_legs


def _leg(mode, name=""):
    return Leg(src=name, dst=name, mode=mode, distance_ly=1.0, fuel=0,
               in_range=True, cooldown_min=0.0, fatigue_after_min=0.0,
               wait_before_min=0.0, t_depart_min=0.0)


def test_empty():
    assert collapse_gate_legs([]) == []


def test_single_gate_is_not_collapsed():
    """One gate is not a run; a "x1" label would be noise."""
    legs = [_leg("gate")]
    assert [n for _l, n in collapse_gate_legs(legs)] == [1]


def test_contiguous_gates_collapse():
    legs = [_leg("gate"), _leg("gate"), _leg("gate")]
    out = collapse_gate_legs(legs)
    assert len(out) == 1
    assert out[0][1] == 3


def test_non_gate_legs_break_the_run():
    legs = [_leg("gate"), _leg("gate"), _leg("jump"), _leg("gate")]
    out = collapse_gate_legs(legs)
    assert [(l.mode, n) for l, n in out] == [
        ("gate", 2), ("jump", 1), ("gate", 1)]


def test_collapsed_run_keeps_first_and_last_endpoints():
    """The row has to read 'from the start of the run to its end'."""
    a, b, c = _leg("gate", "A"), _leg("gate", "B"), _leg("gate", "C")
    a.src, a.dst = "A", "B"
    b.src, b.dst = "B", "C"
    c.src, c.dst = "C", "D"
    (leg, n), = collapse_gate_legs([a, b, c])
    assert leg.src == "A" and leg.dst == "D" and n == 3


def test_other_modes_never_collapse():
    """Two wormholes in a row are two different holes, not one hop twice."""
    legs = [_leg("hole"), _leg("hole")]
    assert [n for _l, n in collapse_gate_legs(legs)] == [1, 1]
