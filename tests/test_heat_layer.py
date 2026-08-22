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
    v._heat_unit = ""
    v._glow = []
    v._glow_cache = {}
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


# -- glow layer ------------------------------------------------------------
# The glow rides the heat layer (no toggle of its own) and only the top
# _GLOW_BANDS of the ramp get one, so "which systems glow" is a property of
# the band maths, not of the painting -- which is what these cover. The
# drawBackground blit itself needs a real viewport and is left to the eye.


def test_only_the_hot_end_glows(view):
    """A cold system keeps its ramp colour but gets no glow behind it."""
    view._make_dots([1, 2, 3])
    view.set_heat({1: 1, 2: 40, 3: 100000}, "Ship kills")

    assert len(view._heat_brushes) == 3, "every system still takes a colour"
    glowing = {sid for sid, _c, _r in view._glow}
    assert 3 in glowing, "the peak system must glow"
    assert 1 not in glowing, "a bottom-band system must not glow"


def test_radius_scales_with_the_value(view):
    view._make_dots([1, 2])
    view.set_heat({1: 5000, 2: 100000}, "Ship kills")

    radii = {sid: r for sid, _c, r in view._glow}
    assert radii[2] > radii[1], "the hotter system needs the bigger glow"
    assert radii[2] <= view._GLOW_MAX_PX


def test_glow_is_sorted_smallest_first(view):
    """Painting order: the biggest glow lands on top, not buried."""
    view._make_dots([1, 2, 3])
    view.set_heat({1: 900, 2: 100000, 3: 9000}, "Ship kills")
    radii = [r for _sid, _c, r in view._glow]
    assert radii == sorted(radii)


def test_clearing_the_layer_clears_the_glow(view):
    view._make_dots([1])
    view.set_heat({1: 5000}, "Ship kills")
    assert view._glow
    view.set_heat(None)
    assert view._glow == []


def test_glow_sprites_are_cached_per_colour_radius_and_dpi(qapp, view):
    """The whole point of the sprite: rasterise once, blit many.

    A fresh gradient per system per frame is what made the rejected halo-item
    layer unusable, so a cache miss on a repeat call is a real regression.
    """
    from PySide6.QtGui import QColor

    red = QColor("#d63b2a")
    first = view._glow_pixmap(red, 12.0, 1.0)
    again = view._glow_pixmap(red, 12.0, 1.0)
    assert again is first, "same colour and radius must reuse the sprite"
    assert len(view._glow_cache) == 1

    view._glow_pixmap(red, 20.0, 1.0)
    view._glow_pixmap(red, 12.0, 2.0)      # hidpi monitor
    assert len(view._glow_cache) == 3

    hidpi = view._glow_cache[(red.rgb(), 12.0, 2.0)]
    assert hidpi.devicePixelRatio() == 2.0
    assert hidpi.width() == 48, "hidpi sprite is rendered at 2x the pixels"


def test_radii_are_quantised_so_the_cache_stays_small(view):
    view._make_dots(list(range(1, 60)))
    view.set_heat({i: i * 137 for i in range(1, 60)}, "Ship kills")
    distinct = {r for _sid, _c, r in view._glow}
    span = view._GLOW_MAX_PX - view._GLOW_MIN_PX
    assert len(distinct) <= span / view._GLOW_STEP_PX + 1


def test_sprite_is_a_centred_circle_at_every_dpi(qapp, view):
    """The glow must be a circle centred on the dot, not a cornered square.

    Regression: the sprite scaled by the device pixel ratio on a painter that
    was already dpr-scaled by ``setDevicePixelRatio``. At dpr 1.0 the extra
    scale is a no-op, so this was invisible to every test and to an offscreen
    render -- and drew a hard-edged quarter circle on a real 1.5x display.
    Anything that only checks dpr 1.0 will not catch it coming back.
    """
    from PySide6.QtGui import QColor

    for dpr in (1.0, 1.5, 2.0):
        img = view._glow_pixmap(QColor("#d63b2a"), 24.0, dpr).toImage()
        n = img.width()
        mid = n // 2
        where = f"at dpr {dpr}"

        assert img.pixelColor(mid, mid).alpha() > 100, f"no glow centre {where}"
        for x, y in ((0, 0), (n - 1, 0), (0, n - 1), (n - 1, n - 1)):
            assert img.pixelColor(x, y).alpha() == 0, f"square corner {where}"

        # Equal distance along all four axes must be equally bright, or the
        # gradient centre has drifted off the middle of the sprite. The
        # sprite is an even number of pixels wide, so the true centre falls
        # on a pixel boundary: mid+q and mid-1-q are the pair equidistant
        # from it, while mid-q would sit a whole pixel closer and read
        # brighter for reasons that have nothing to do with the glow.
        q = n // 4
        alphas = [img.pixelColor(mid + q, mid).alpha(),
                  img.pixelColor(mid - 1 - q, mid).alpha(),
                  img.pixelColor(mid, mid + q).alpha(),
                  img.pixelColor(mid, mid - 1 - q).alpha()]
        assert max(alphas) - min(alphas) <= 2, f"off-centre glow {where}: {alphas}"


def test_sprite_covers_the_radius_it_claims(qapp, view):
    """Physical size tracks dpr; logical size stays the radius it was asked for."""
    from PySide6.QtGui import QColor

    pm = view._glow_pixmap(QColor("#d63b2a"), 24.0, 1.5)
    assert pm.width() == 72, "buffer must be rendered at 1.5x the pixels"
    assert pm.width() / pm.devicePixelRatio() == 48, "logical size is 2 x radius"


# -- hover stat line -------------------------------------------------------


def test_hover_stat_reads_value_then_unit(view):
    """The line under the system name: "1,204 jumps (1h)"."""
    view._make_dots([1, 2])
    view.set_heat({1: 1204, 2: 37}, "Gate traffic, last hour", "jumps (1h)")
    assert view.heat_stat(1) == "1,204 jumps (1h)"
    assert view.heat_stat(2) == "37 jumps (1h)"


def test_hover_stat_keeps_a_decimal_for_small_fractional_metrics(view):
    """ADM and the industry index are fractional; counts are not."""
    view._make_dots([1, 2])
    view.set_heat({1: 4.7, 2: 6.0}, "Sovereignty ADM", "ADM")
    assert view.heat_stat(1) == "4.7 ADM"
    assert view.heat_stat(2) == "6 ADM", "a whole number should not gain a .0"


def test_hover_stat_is_empty_for_a_system_with_no_reading(view):
    """ESI omits quiet systems, so silence is honest and "0 jumps" is not."""
    view._make_dots([1, 2])
    view.set_heat({1: 500}, "Gate traffic, last hour", "jumps (1h)")
    assert view.heat_stat(2) == ""


def test_hover_stat_disappears_with_the_heat_layer(view):
    view._make_dots([1])
    view.set_heat({1: 500}, "Gate traffic, last hour", "jumps (1h)")
    assert view.heat_stat(1)

    view._overlay_on["heat"] = False
    assert view.heat_stat(1) == "", "no heat layer, no stat line"

    view._overlay_on["heat"] = True
    view.set_heat(None)
    assert view.heat_stat(1) == "", "cleared layer, no stat line"


def test_hover_stat_survives_a_layer_without_a_unit(view):
    """An unmapped layer key still shows the number rather than crashing."""
    view._make_dots([1])
    view.set_heat({1: 42}, "Something new")
    assert view.heat_stat(1) == "42"
