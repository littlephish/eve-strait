import types

from eve_strait.esi import client as client_mod


class SpyTransport:
    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload if payload is not None else []

    def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return types.SimpleNamespace(json=lambda: self.payload, headers={},
                                     status_code=200, from_cache=False)


def spy(monkeypatch, payload=None):
    t = SpyTransport(payload)
    monkeypatch.setattr(client_mod, "get_transport", lambda: t)
    return t


def test_system_activity_defaults_to_background_and_no_force(monkeypatch):
    t = spy(monkeypatch)
    client_mod.system_activity()
    for _, kwargs in t.calls:
        assert kwargs["priority"] == "background"
        assert not kwargs.get("force")


def test_system_activity_forwards_force_and_priority(monkeypatch):
    t = spy(monkeypatch)
    client_mod.system_activity(force=True, priority="interactive")
    assert t.calls, "expected at least one request"
    for _, kwargs in t.calls:
        assert kwargs["priority"] == "interactive"
        assert kwargs["force"] is True


def test_sovereignty_defense_forwards_force_and_priority(monkeypatch):
    t = spy(monkeypatch)
    client_mod.sovereignty_defense(force=True, priority="interactive")
    path, kwargs = t.calls[0]
    assert path == "/sovereignty/structures/"
    assert kwargs["priority"] == "interactive"
    assert kwargs["force"] is True


def test_industry_indices_forwards_force_and_priority(monkeypatch):
    t = spy(monkeypatch)
    client_mod.industry_indices(force=True, priority="interactive")
    path, kwargs = t.calls[0]
    assert path == "/industry/systems/"
    assert kwargs["priority"] == "interactive"
    assert kwargs["force"] is True


def test_sovereignty_defense_defaults_unchanged(monkeypatch):
    t = spy(monkeypatch)
    client_mod.sovereignty_defense()
    _, kwargs = t.calls[0]
    assert kwargs["priority"] == "background"
    assert not kwargs.get("force")
