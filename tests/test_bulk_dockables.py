import types

import requests

from eve_strait.esi import client as client_mod


class StubClient:
    """Stands in for EsiClient: one canned answer per character."""

    answers = {}
    instances = {}

    def __init__(self, token, client_id):
        self.token = token
        StubClient.instances[token.character_id] = self

    def dockable_locations(self, progress=None):
        return StubClient.answers[self.token.character_id]()


def token(cid, name):
    return types.SimpleNamespace(character_id=cid, character_name=name,
                                 expired=False, access_token="t")


def dock(name):
    return client_mod.Dockable(1, name, 2, "station")


def setup_stub(monkeypatch, answers):
    StubClient.answers = answers
    StubClient.instances = {}
    monkeypatch.setattr(client_mod, "EsiClient", StubClient)
    monkeypatch.setattr(client_mod, "save_dockables", lambda cid, d: None)


def test_every_character_is_fetched(monkeypatch):
    setup_stub(monkeypatch, {
        1: lambda: [dock("Jita 4-4")],
        2: lambda: [dock("Amarr VIII")],
    })
    tokens = {1: token(1, "A"), 2: token(2, "B")}
    results, notes = client_mod.load_all_dockables(tokens, "cid")
    assert set(results) == {1, 2}
    assert results[1][0].name == "Jita 4-4"
    assert notes == []


def test_results_are_saved_per_character(monkeypatch):
    saved = {}
    setup_stub(monkeypatch, {1: lambda: [dock("Jita 4-4")]})
    monkeypatch.setattr(client_mod, "save_dockables",
                        lambda cid, d: saved.__setitem__(cid, d))
    client_mod.load_all_dockables({1: token(1, "A")}, "cid")
    assert saved[1][0].name == "Jita 4-4"


def test_one_failure_does_not_abort_the_rest(monkeypatch):
    def boom():
        raise requests.HTTPError("403", response=types.SimpleNamespace(
            status_code=403))

    setup_stub(monkeypatch, {1: boom, 2: lambda: [dock("Amarr VIII")]})
    tokens = {1: token(1, "Denied"), 2: token(2, "Fine")}
    results, notes = client_mod.load_all_dockables(tokens, "cid")
    assert 2 in results and results[2][0].name == "Amarr VIII"
    assert 1 not in results
    assert any("Denied" in n and "403" in n for n in notes)


def test_torn_asset_read_is_reported_not_raised(monkeypatch):
    def torn():
        raise client_mod.AssetsChangedDuringFetch("changed")

    setup_stub(monkeypatch, {1: torn})
    results, notes = client_mod.load_all_dockables({1: token(1, "Busy")}, "cid")
    assert results == {}
    assert any("Busy" in n for n in notes)


def test_progress_names_each_character(monkeypatch):
    seen = []
    setup_stub(monkeypatch, {1: lambda: [], 2: lambda: []})
    tokens = {1: token(1, "Alpha"), 2: token(2, "Beta")}
    client_mod.load_all_dockables(tokens, "cid", progress=seen.append)
    assert any("Alpha" in m for m in seen)
    assert any("Beta" in m for m in seen)
