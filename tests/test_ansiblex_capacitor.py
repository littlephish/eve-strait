"""Ansiblex capacitor cost per activation.

Zone is set by the DESTINATION's distance from the owning alliance's capital
system; the distance between the two gates is irrelevant. Zone 1 is free.
"""
import pytest

from eve_strait.jump import ansiblex


@pytest.mark.parametrize("ly,zone", [
    (0.0, 1), (5.0, 1), (5.1, 2), (10.0, 2), (10.1, 3),
    (15.0, 3), (15.1, 4), (20.0, 4), (20.1, 5), (99.0, 5),
])
def test_zone_boundaries(ly, zone):
    assert ansiblex.zone_for(ly) == zone


def test_zone_one_is_free():
    assert ansiblex.capacitor_cost("Jump Freighter", 3.0) == 0.0


def test_jump_freighter_is_the_cheapest_tier():
    assert ansiblex.capacitor_cost("Jump Freighter", 7.0) == 2.0


def test_black_ops_scales_with_zone():
    assert ansiblex.capacitor_cost("Black Ops", 7.0) == 36.0
    assert ansiblex.capacitor_cost("Black Ops", 25.0) == 270.0


def test_rorqual_zones_two_and_three_are_special_cases():
    """CCP's table overrides both: 57.00 not 19x2=38, and 104.50 not 19x6=114.
    Zones 4 and 5 follow the multiplier normally."""
    assert ansiblex.capacitor_cost("Capital Industrial", 7.0) == 57.0
    assert ansiblex.capacitor_cost("Capital Industrial", 12.0) == 104.5
    assert ansiblex.capacitor_cost("Capital Industrial", 17.0) == 171.0
    assert ansiblex.capacitor_cost("Capital Industrial", 25.0) == 285.0


def test_unmodelled_hull_class_is_unknown():
    """Subcaps collapse to one generic entry, so per-class cost is unknowable."""
    assert ansiblex.capacitor_cost("Subcapital", 7.0) is None


def test_banned_hull_has_no_cost():
    assert ansiblex.capacitor_cost("Titan", 7.0) is None


def test_unknown_is_not_free():
    """None must never be confused with zero: zone 1 genuinely costs nothing,
    an unmodelled hull is simply not known, and a UI showing "0 TJ" for a
    Titan would be a lie."""
    assert ansiblex.capacitor_cost("Titan", 7.0) is None
    assert ansiblex.capacitor_cost("Jump Freighter", 1.0) == 0.0
