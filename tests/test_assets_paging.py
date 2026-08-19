import types

import pytest

from eve_strait.esi import client as client_mod


class PagedTransport:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append(kwargs.get("params"))
        page = (kwargs.get("params") or {}).get("page", 1)
        body, last_modified = self.pages[page - 1]
        return types.SimpleNamespace(
            json=lambda: body, status_code=200, from_cache=False,
            headers={"X-Pages": str(len(self.pages)),
                     "last-modified": last_modified})


def make_client(monkeypatch, pages):
    stub = PagedTransport(pages)
    monkeypatch.setattr(client_mod, "get_transport", lambda: stub)
    token = types.SimpleNamespace(access_token="tok", character_id=42,
                                  expired=False, character_name="Pilot")
    return client_mod.EsiClient(token, "cid"), stub


STAMP = "Wed, 19 Aug 2026 12:00:00 GMT"
LATER = "Wed, 19 Aug 2026 13:00:00 GMT"


def test_all_pages_are_concatenated(monkeypatch):
    c, _ = make_client(monkeypatch, [([{"a": 1}], STAMP), ([{"b": 2}], STAMP)])
    assert c.assets() == [{"a": 1}, {"b": 2}]


def test_pages_that_disagree_are_rejected(monkeypatch):
    # CCP's advice: if last-modified shifts mid-walk the data refreshed
    # underneath us and the concatenation is a torn read.
    c, _ = make_client(monkeypatch, [([{"a": 1}], STAMP), ([{"b": 2}], LATER)])
    with pytest.raises(client_mod.AssetsChangedDuringFetch):
        c.assets()


def test_single_page_needs_no_stamp(monkeypatch):
    c, _ = make_client(monkeypatch, [([{"a": 1}], "")])
    assert c.assets() == [{"a": 1}]


def test_force_is_forwarded_to_every_page(monkeypatch):
    c, stub = make_client(monkeypatch, [([{"a": 1}], STAMP), ([{"b": 2}], STAMP)])
    captured = []
    original = stub.get

    def spy(path, **kwargs):
        captured.append(kwargs.get("force"))
        return original(path, **kwargs)

    stub.get = spy
    c.assets(force=True)
    assert captured == [True, True]
