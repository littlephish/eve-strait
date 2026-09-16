"""Alliance capital systems, which set Ansiblex capacitor zones.

/sovereignty/systems carries is_capital_system on the alliance claim, so one
public call gives every alliance's capital. Parsing is kept separate from
fetching so it tests without network.
"""
import json
from pathlib import Path

from eve_strait.esi import client

FIXTURE = Path(__file__).parent / "data" / "sovereignty_systems.json"


def test_capital_systems_are_extracted():
    payload = json.loads(FIXTURE.read_text())
    assert client.capital_systems(payload) == {
        99003581: 30000239,
        1900696668: 30004600,
    }


def test_non_capital_and_faction_claims_are_ignored():
    payload = json.loads(FIXTURE.read_text())
    caps = client.capital_systems(payload)
    assert 30000208 not in caps.values()
    assert 500007 not in caps


def test_empty_payload_is_empty():
    assert client.capital_systems({}) == {}
