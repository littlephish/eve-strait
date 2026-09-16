"""ESI has two disjoint route sets and we need one endpoint from each.

/sovereignty/systems exists only on the compatibility-dated base and requires
the date; /sovereignty/map/ exists only under /latest. So the transport picks
a base per path.
"""
from eve_strait import config
from eve_strait.esi import transport


def test_legacy_path_uses_the_versioned_base():
    url, params = transport.url_for("/sovereignty/map/")
    assert url == f"{config.ESI_BASE}/sovereignty/map/"
    assert params == {}


def test_compat_path_uses_the_dated_base():
    url, params = transport.url_for("/sovereignty/systems")
    assert url == f"{config.ESI_COMPAT_BASE}/sovereignty/systems"
    assert params == {"compatibility_date": config.ESI_COMPATIBILITY_DATE}


def test_compatibility_date_is_pinned():
    """An unpinned date silently changes the route set under us."""
    assert config.ESI_COMPATIBILITY_DATE == "2026-08-18"
