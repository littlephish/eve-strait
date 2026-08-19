import types

from eve_strait.esi import client as client_mod


class StubTransport:
    def __init__(self):
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return types.SimpleNamespace(json=lambda: {"ok": True}, headers={},
                                     status_code=200, from_cache=False)


def make_client(monkeypatch):
    stub = StubTransport()
    monkeypatch.setattr(client_mod, "get_transport", lambda: stub)
    token = types.SimpleNamespace(access_token="tok", character_id=42,
                                  expired=False, character_name="Pilot")
    return client_mod.EsiClient(token, "client-id"), stub


def test_get_passes_character_id_for_bucket_keying(monkeypatch):
    c, stub = make_client(monkeypatch)
    c.location()
    path, kwargs = stub.calls[0]
    assert path == "/characters/42/location/"
    assert kwargs["character_id"] == 42


def test_get_passes_the_bearer_token(monkeypatch):
    c, stub = make_client(monkeypatch)
    c.location()
    _, kwargs = stub.calls[0]
    assert kwargs["headers"]["Authorization"] == "Bearer tok"


def test_priority_defaults_to_interactive(monkeypatch):
    c, stub = make_client(monkeypatch)
    c.location()
    _, kwargs = stub.calls[0]
    assert kwargs["priority"] == "interactive"


def test_background_priority_is_forwarded(monkeypatch):
    c, stub = make_client(monkeypatch)
    c.location(priority="background")
    _, kwargs = stub.calls[0]
    assert kwargs["priority"] == "background"


def test_module_level_calls_pass_no_character_id(monkeypatch):
    stub = StubTransport()
    monkeypatch.setattr(client_mod, "get_transport", lambda: stub)
    stub.get = lambda path, **kw: (stub.calls.append((path, kw)) or
                                   types.SimpleNamespace(
                                       json=lambda: [], headers={},
                                       status_code=200, from_cache=False))
    client_mod.incursions()
    path, kwargs = stub.calls[0]
    assert path == "/incursions/"
    assert kwargs.get("character_id") is None
