"""Who may use an Ansiblex after Cradle of War (2026-09-22).

Capitals and supercapitals lose access entirely. The Rorqual is CCP's sole
industrial exception, and jump freighters and Black Ops were never capitals
for this purpose -- both appear in CCP's own capacitor cost table.
"""
import pytest

from eve_strait.data import docking
from eve_strait.data.ships import SHIPS, SHIPS_BY_NAME


@pytest.mark.parametrize("hull", sorted(docking.ANSIBLEX_BANNED_HULLS))
def test_every_banned_hull_class_exists_and_is_refused(hull):
    """Also guards against a typo in the ban list: a name that matches no
    ship would silently ban nothing."""
    ships = [s for s in SHIPS if s.hull_class == hull]
    assert ships, f"no ship has hull_class {hull!r}"
    for s in ships:
        assert docking.ansiblex_allowed(s) is False


@pytest.mark.parametrize("name", ["Rorqual", "Ark", "Rhea", "Redeemer", "Widow"])
def test_permitted_hulls_keep_access(name):
    assert docking.ansiblex_allowed(SHIPS_BY_NAME[name]) is True


def test_rorqual_is_the_only_capital_industrial():
    """The exception is exact only while this stays true."""
    hulls = [s.name for s in SHIPS if s.hull_class == "Capital Industrial"]
    assert hulls == ["Rorqual"]


def test_generic_subcapital_is_allowed():
    subcaps = [s for s in SHIPS if s.hull_class == "Subcapital"]
    assert subcaps
    for s in subcaps:
        assert docking.ansiblex_allowed(s) is True
