"""Ignoring a wormhole has to expire, because the wormhole does.

A signature is gone within a day at the outside -- 24 hours is the longest
any natural wormhole lives (B274 to hi-sec); most manage 16. So a permanent
"never route this" is meaningless, and worse than meaningless: the same two
systems can be joined again tomorrow by a completely different hole, and a
stale entry would silently refuse it.
"""
import time

from eve_strait.esi import evescout


def test_max_lifetime_is_the_longest_a_wormhole_lives():
    assert evescout.MAX_LIFETIME_HOURS == 24.0


def test_expiry_defaults_to_the_ceiling():
    now = 1_000_000.0
    got = evescout.ignore_expiry({}, now=now)
    assert got == now + 24 * 3600


def test_expiry_never_outlives_the_hole_itself():
    """A hole with 3 hours left cannot matter in 4."""
    now = 1_000_000.0
    got = evescout.ignore_expiry({"hours": 3}, now=now)
    assert got == now + 3 * 3600


def test_reported_life_longer_than_the_ceiling_is_capped():
    now = 1_000_000.0
    assert evescout.ignore_expiry({"hours": 99}, now=now) == now + 24 * 3600


def test_active_ignores_drops_expired_entries():
    now = 1_000.0
    ignored = {(1, 2): {"until": now - 1, "sig": None},
               (3, 4): {"until": now + 60, "sig": None}}
    assert evescout.active_ignores(ignored, {}, now=now) == {(3, 4)}


def test_active_ignores_releases_a_replaced_signature():
    """Same pair, different hole: the new one was never rejected."""
    now = 1_000.0
    ignored = {(1, 2): {"until": now + 600, "sig": "ABC-123"}}
    same = {(1, 2): {"sigs": {1: "ABC-123"}}}
    other = {(1, 2): {"sigs": {1: "XYZ-789"}}}
    assert evescout.active_ignores(ignored, same, now=now) == {(1, 2)}
    assert evescout.active_ignores(ignored, other, now=now) == set()


def test_unknown_signature_falls_back_to_the_pair():
    """Wanderer edges carry no sigs; the ignore still has to work."""
    now = 1_000.0
    ignored = {(1, 2): {"until": now + 600, "sig": None}}
    assert evescout.active_ignores(ignored, {(1, 2): {"sigs": {}}},
                                   now=now) == {(1, 2)}


def test_prunes_pairs_that_no_longer_exist():
    """The hole collapsed; nothing left to ignore."""
    now = 1_000.0
    ignored = {(1, 2): {"until": now + 600, "sig": "ABC-123"}}
    assert evescout.active_ignores(ignored, {}, now=now) == {(1, 2)}
