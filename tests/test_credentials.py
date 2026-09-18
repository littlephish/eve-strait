"""A Tripwire password never goes in config.json.

Everything else this app stores is a scoped, revocable ESI token. A Tripwire
password is neither: it is full account access, and if the user reuses it,
worse than that. It belongs in the operating system's credential store --
Windows Credential Manager, macOS Keychain, or SecretService on Linux --
which is what keyring picks per platform.

Absence of keyring must degrade to "cannot remember it", never to "write it
to disk anyway".
"""
import pytest

from eve_strait import config


def test_url_and_username_are_ordinary_settings(monkeypatch):
    saved = {}
    monkeypatch.setattr(config, "load_config", lambda: dict(saved))
    monkeypatch.setattr(config, "save_config", lambda cfg: saved.update(cfg))
    config.set_tripwire("https://tripwire.example/", "  Pilot Name  ")
    assert saved["tripwire_url"] == "https://tripwire.example"
    assert saved["tripwire_user"] == "Pilot Name"
    assert config.get_tripwire_url() == "https://tripwire.example"
    assert config.get_tripwire_user() == "Pilot Name"


def test_password_is_never_written_to_the_config(monkeypatch):
    saved = {}
    monkeypatch.setattr(config, "load_config", lambda: dict(saved))
    monkeypatch.setattr(config, "save_config", lambda cfg: saved.update(cfg))
    monkeypatch.setattr(config, "_keyring", lambda: None)   # no backend
    config.set_tripwire_password("hunter2")
    blob = repr(saved)
    assert "hunter2" not in blob
    assert not any("pass" in k.lower() for k in saved)


def test_password_round_trips_through_the_keyring(monkeypatch):
    store = {}

    class FakeKeyring:
        def set_password(self, service, user, password):
            store[(service, user)] = password

        def get_password(self, service, user):
            return store.get((service, user))

        def delete_password(self, service, user):
            store.pop((service, user), None)

    monkeypatch.setattr(config, "_keyring", lambda: FakeKeyring())
    monkeypatch.setattr(config, "get_tripwire_user", lambda: "Pilot")
    config.set_tripwire_password("hunter2")
    assert config.get_tripwire_password() == "hunter2"
    assert store == {(config.KEYRING_SERVICE, "Pilot"): "hunter2"}


def test_clearing_the_password_removes_it(monkeypatch):
    store = {("eve-strait", "Pilot"): "hunter2"}

    class FakeKeyring:
        def set_password(self, s, u, p): store[(s, u)] = p
        def get_password(self, s, u): return store.get((s, u))
        def delete_password(self, s, u): store.pop((s, u), None)

    monkeypatch.setattr(config, "_keyring", lambda: FakeKeyring())
    monkeypatch.setattr(config, "get_tripwire_user", lambda: "Pilot")
    monkeypatch.setattr(config, "KEYRING_SERVICE", "eve-strait")
    config.set_tripwire_password("")
    assert config.get_tripwire_password() is None


def test_missing_keyring_degrades_rather_than_writing_to_disk(monkeypatch):
    monkeypatch.setattr(config, "_keyring", lambda: None)
    monkeypatch.setattr(config, "get_tripwire_user", lambda: "Pilot")
    assert config.get_tripwire_password() is None
    config.set_tripwire_password("hunter2")      # must not raise
    assert config.get_tripwire_password() is None


def test_a_broken_keyring_is_not_fatal(monkeypatch):
    """A locked or misconfigured store must cost the feature, not the app."""
    class Broken:
        def set_password(self, *a): raise RuntimeError("locked")
        def get_password(self, *a): raise RuntimeError("locked")
        def delete_password(self, *a): raise RuntimeError("locked")

    monkeypatch.setattr(config, "_keyring", lambda: Broken())
    monkeypatch.setattr(config, "get_tripwire_user", lambda: "Pilot")
    assert config.get_tripwire_password() is None
    config.set_tripwire_password("hunter2")


def test_password_without_a_username_is_not_stored(monkeypatch):
    """The username is the key; storing under an empty one is a leak."""
    calls = []

    class FakeKeyring:
        def set_password(self, s, u, p): calls.append((s, u, p))
        def get_password(self, s, u): return None
        def delete_password(self, s, u): pass

    monkeypatch.setattr(config, "_keyring", lambda: FakeKeyring())
    monkeypatch.setattr(config, "get_tripwire_user", lambda: "")
    config.set_tripwire_password("hunter2")
    assert calls == []
