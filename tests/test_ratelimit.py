import pytest

from eve_strait.esi.ratelimit import (Decision, Limit, RateLimitGovernor,
                                      parse_limit, route_key)


@pytest.mark.parametrize("raw,expected", [
    ("150/15m", Limit(150, 900)),
    ("1000/15m", Limit(1000, 900)),
    ("1000/1h", Limit(1000, 3600)),
    ("60/30s", Limit(60, 30)),
])
def test_parse_limit_understands_ccp_format(raw, expected):
    assert parse_limit(raw) == expected


@pytest.mark.parametrize("raw", ["", "nonsense", "150", "150/15x", "abc/15m", None])
def test_parse_limit_returns_none_on_junk(raw):
    assert parse_limit(raw) is None


def test_route_key_collapses_numeric_ids():
    assert route_key("/characters/12345/assets/") == "/characters/{id}/assets/"
    assert route_key("/universe/stations/60003760/") == "/universe/stations/{id}/"


def test_route_key_leaves_static_paths_alone():
    assert route_key("/sovereignty/map/") == "/sovereignty/map/"

ASSETS = "/characters/12345/assets/"


def gov_at(remaining, limit="1000/15m", clock=lambda: 1000.0):
    """A governor that has already seen one response for ASSETS."""
    g = RateLimitGovernor(clock=clock)
    g.observe(ASSETS, 12345, {
        "X-Ratelimit-Group": "assets",
        "X-Ratelimit-Limit": limit,
        "X-Ratelimit-Remaining": str(remaining),
    })
    return g


def test_unknown_route_proceeds_without_delay():
    g = RateLimitGovernor(clock=lambda: 1000.0)
    assert g.check(ASSETS, 12345, "background") == Decision("proceed")


def test_plenty_of_budget_proceeds():
    assert gov_at(800).check(ASSETS, 12345, "background").action == "proceed"


def test_below_half_paces_by_remaining_requests():
    # 400 tokens left = 200 requests at 2 tokens each, across a 900s window.
    d = gov_at(400).check(ASSETS, 12345, "interactive")
    assert d.action == "wait"
    assert d.seconds == pytest.approx(4.5)


def test_pacing_delay_is_capped_at_60s():
    d = gov_at(20).check(ASSETS, 12345, "interactive")
    assert d.seconds == 60.0


def test_below_reserve_floor_background_is_declined():
    d = gov_at(50).check(ASSETS, 12345, "background")   # 5% of 1000
    assert d.action == "decline"


def test_below_reserve_floor_interactive_still_spends():
    d = gov_at(50).check(ASSETS, 12345, "interactive")
    assert d.action == "wait"


def test_state_is_keyed_per_character():
    g = gov_at(50)
    # A different character is a different bucket and must be unaffected.
    assert g.check("/characters/999/assets/", 999, "background") == Decision("proceed")


def test_poll_interval_uses_floor_when_budget_is_generous():
    assert gov_at(1000).poll_interval(ASSETS, 12345) == 30.0


def test_poll_interval_derives_from_a_tight_limit():
    # 150 tokens: half to background = 75 tokens = 37.5 requests over 900s.
    g = gov_at(150, limit="150/15m")
    assert g.poll_interval(ASSETS, 12345) == pytest.approx(30.0)


def test_poll_interval_never_dips_below_the_floor():
    assert gov_at(5000, limit="5000/15m").poll_interval(ASSETS, 12345) == 30.0


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_park_blocks_background_until_it_expires():
    clock = FakeClock()
    g = gov_at(1000, clock=clock)
    g.park(ASSETS, 12345, 30)
    assert g.check(ASSETS, 12345, "background").action == "decline"
    clock.t += 31
    assert g.check(ASSETS, 12345, "background").action == "proceed"


def test_short_park_makes_interactive_wait_the_remainder():
    clock = FakeClock()
    g = gov_at(1000, clock=clock)
    g.park(ASSETS, 12345, 30)
    clock.t += 10
    d = g.check(ASSETS, 12345, "interactive")
    assert d.action == "wait"
    assert d.seconds == pytest.approx(20.0)


def test_long_park_is_reported_not_slept_through():
    # 15 minutes is too long to freeze a button on. The transport turns a
    # wait longer than MAX_INTERACTIVE_WAIT into a RateLimited error.
    g = gov_at(1000)
    g.park(ASSETS, 12345, 900)
    d = g.check(ASSETS, 12345, "interactive")
    assert d.action == "wait"
    assert d.seconds > 60.0


def test_error_limit_parks_every_group():
    clock = FakeClock()
    g = gov_at(1000, clock=clock)
    g.observe_errors({"X-ESI-Error-Limit-Remain": "3",
                      "X-ESI-Error-Limit-Reset": "45"})
    assert g.check(ASSETS, 12345, "background").action == "decline"
    # A completely unrelated route is parked too: the error budget is global.
    assert g.check("/sovereignty/map/", None, "background").action == "decline"
    clock.t += 46
    assert g.check("/sovereignty/map/", None, "background").action == "proceed"


def test_healthy_error_budget_parks_nothing():
    g = gov_at(1000)
    g.observe_errors({"X-ESI-Error-Limit-Remain": "95",
                      "X-ESI-Error-Limit-Reset": "45"})
    assert g.check(ASSETS, 12345, "background").action == "proceed"


def test_poll_interval_respects_a_custom_floor():
    g = gov_at(150, limit="150/15m")
    assert g.poll_interval(ASSETS, 12345, floor=5.0) == pytest.approx(24.0)


def test_poll_interval_for_an_unobserved_route_is_the_floor():
    g = RateLimitGovernor(clock=lambda: 1000.0)
    assert g.poll_interval("/characters/1/location/", 1) == 30.0
