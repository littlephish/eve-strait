"""Regression tests for the heat layer surviving the map-build race.

These need Qt, unlike the rest of the suite. The bug they cover is precisely
an ordering-of-Qt-state problem — heat values arriving before the map's dots
exist — so there is no pure-logic version of it. Runs offscreen; no window
is ever shown.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def view(qapp):
    """A MapView with three dots, without loading the real universe."""
    from PySide6.QtWidgets import QGraphicsEllipseItem

    from eve_strait.ui.map_view import MapView

    v = MapView.__new__(MapView)          # skip __init__: it wants a universe
    v._dots = {}
    v._sec_brushes = {}
    v._heat_brushes = {}
    v._heat_values = {}
    v._heat_label = ""
    v._heat_max = 0.0
    v._overlay_on = {"heat": True}
    v.viewport = lambda: type("V", (), {"update": staticmethod(lambda: None)})()
    def make_dots(ids):
        from PySide6.QtGui import QBrush, QColor
        for i in ids:
            v._dots[i] = QGraphicsEllipseItem()
            # Clearing the layer falls back to these, so a fixture without
            # them fails in _apply_heat rather than in the code under test.
            v._sec_brushes[i] = QBrush(QColor("#404040"))

    v._make_dots = make_dots
    return v


def test_heat_applies_when_dots_already_exist(view):
    view._make_dots([30000142, 30002187])
    view.set_heat({30000142: 50, 30002187: 10}, "Ship kills")
    assert len(view._heat_brushes) == 2


def test_heat_values_survive_arriving_before_the_dots(view):
    """The regression: cached intel beats the map build.

    Before caching, intel took seconds over the network and always landed
    after _build(). Served from sqlite it arrives in ~160ms and wins the
    race, so set_heat() used to discard every value it could not yet place.
    """
    view.set_heat({30000142: 50, 30002187: 10}, "Ship kills")
    assert view._heat_brushes == {}          # nothing to paint yet, correctly

    view._make_dots([30000142, 30002187])
    view._recompute_heat()                   # what _build() must now call
    assert len(view._heat_brushes) == 2, "heat values were thrown away"


def test_clearing_the_layer_still_clears(view):
    view._make_dots([30000142])
    view.set_heat({30000142: 5}, "Ship kills")
    view.set_heat(None)
    assert view._heat_brushes == {}


def test_values_for_unknown_systems_are_ignored(view):
    view._make_dots([30000142])
    view.set_heat({30000142: 5, 99999999: 900}, "Ship kills")
    assert set(view._heat_brushes) == {30000142}


def test_zero_and_negative_values_are_not_painted(view):
    view._make_dots([30000142, 30002187])
    view.set_heat({30000142: 0, 30002187: 3}, "Ship kills")
    assert set(view._heat_brushes) == {30002187}
