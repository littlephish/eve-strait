"""Bridge records gained an owner; legacy pairs must still load.

The stored shape was [[nameA, nameB], ...]. It is now a list of records so
the router can tell whose alliance a gate belongs to -- the bridge list is
global config while alliance membership is per character, so without this a
route can be planned over an alt's alliance's gates.
"""
from eve_strait import config


def test_legacy_pairs_migrate_as_untrusted_manual_entries(monkeypatch):
    monkeypatch.setattr(config, "load_config",
                        lambda: {"bridges": [["Aaa", "Bbb"], ["Ccc", "Ddd"]]})
    rows = config.get_bridges()
    assert rows == [
        {"a": "Aaa", "b": "Bbb", "alliance_id": None, "source": "manual"},
        {"a": "Ccc", "b": "Ddd", "alliance_id": None, "source": "manual"},
    ]


def test_records_round_trip(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {"bridges": [
        {"a": "Aaa", "b": "Bbb", "alliance_id": 99, "source": "esi"}]})
    rows = config.get_bridges()
    assert rows[0]["alliance_id"] == 99
    assert rows[0]["source"] == "esi"


def test_malformed_rows_are_dropped(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {"bridges": [
        ["OnlyOne"], [], {"a": "Aaa"}, None, ["Aaa", "Bbb"]]})
    assert config.get_bridges() == [
        {"a": "Aaa", "b": "Bbb", "alliance_id": None, "source": "manual"}]


def test_missing_key_is_empty(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {})
    assert config.get_bridges() == []
