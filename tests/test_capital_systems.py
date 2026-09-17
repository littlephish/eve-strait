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


def test_parse_sovereignty_extracts_owners_and_capitals():
    """One call now serves both the sov map and the Ansiblex zones."""
    payload = json.loads(FIXTURE.read_text())
    owners, capitals = client.parse_sovereignty(payload)
    assert owners[30000208] == (99003581, "alliance")
    assert owners[30000001] == (500007, "faction")
    assert 30004601 not in owners          # empty claim
    assert capitals == {99003581: 30000239, 1900696668: 30004600}


def test_alliance_claim_prefers_alliance_over_its_corporation():
    """The alliance claim carries corporation_id too; alliance wins, matching
    the precedence /sovereignty/map/ had."""
    payload = json.loads(FIXTURE.read_text())
    owners, _ = client.parse_sovereignty(payload)
    assert owners[30000239] == (99003581, "alliance")
