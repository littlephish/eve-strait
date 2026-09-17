"""Which scouted holes are worth routing over.

Mass and size are already enforced by fits(); these are the judgement
calls a pilot makes on top: how stale is too stale, and whether to trust a
hole that is end of life or mass-critical.
"""
from datetime import datetime, timedelta, timezone

from eve_strait.esi import evescout


def _iso(minutes_ago):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_defaults_allow_everything():
    assert evescout.usable({}) is True


def test_max_age_rejects_stale():
    fresh = {"updated_at": _iso(10), "updated_at_all": [_iso(10)]}
    stale = {"updated_at": _iso(200), "updated_at_all": [_iso(200)]}
    assert evescout.usable(fresh, max_age_min=60) is True
    assert evescout.usable(stale, max_age_min=60) is False


def test_unknown_age_is_rejected_when_a_limit_is_set():
    """Asking for fresh data means unknown does not qualify."""
    assert evescout.usable({}, max_age_min=60) is False
    assert evescout.usable({}, max_age_min=None) is True


def test_end_of_life_filter():
    eol = {"life": "end of life"}
    assert evescout.usable(eol, allow_eol=True) is True
    assert evescout.usable(eol, allow_eol=False) is False
    assert evescout.usable({"life": "fresh"}, allow_eol=False) is True


def test_mass_status_filter():
    assert evescout.usable({"mass": "critical"},
                           allow_reduced_mass=False) is False
    assert evescout.usable({"mass": "reduced"},
                           allow_reduced_mass=False) is False
    assert evescout.usable({"mass": "critical"},
                           allow_reduced_mass=True) is True


def test_unknown_life_and_mass_are_allowed():
    """EVE-Scout reports neither; absence must not read as bad."""
    assert evescout.usable({}, allow_eol=False,
                           allow_reduced_mass=False) is True
