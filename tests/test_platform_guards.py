"""In-place updating is Windows-only, and never inside a Flatpak.

The mechanism swaps a program folder by polling a .exe file lock, with a
powershell.exe fallback and CREATE_BREAKAWAY_FROM_JOB. None of that exists
elsewhere, and inside a Flatpak the feature is not merely unavailable -- the
sandbox is read-only and Flatpak does the updating.

Checking and installing are separate questions. macOS can usefully be told a
new version exists even though it must never write over a signed .app, which
would invalidate the signature and leave Gatekeeper refusing to launch it.
"""
from eve_strait import update


def test_install_unsupported_off_windows(monkeypatch):
    monkeypatch.setattr(update.sys, "platform", "linux")
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    assert update.update_install_supported() is False


def test_install_unsupported_inside_a_flatpak(monkeypatch):
    monkeypatch.setattr(update.sys, "platform", "linux")
    monkeypatch.setenv("FLATPAK_ID", "io.github.littlephish.EveStrait")
    assert update.update_install_supported() is False


def test_install_supported_on_windows(monkeypatch):
    monkeypatch.setattr(update.sys, "platform", "win32")
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    assert update.update_install_supported() is True


def test_windows_does_not_install_inside_a_flatpak(monkeypatch):
    """Belt and braces: FLATPAK_ID wins regardless of reported platform."""
    monkeypatch.setattr(update.sys, "platform", "win32")
    monkeypatch.setenv("FLATPAK_ID", "io.github.littlephish.EveStrait")
    assert update.update_install_supported() is False


def test_checking_is_allowed_off_windows(monkeypatch):
    """macOS should still be told a new version exists."""
    monkeypatch.setattr(update.sys, "platform", "darwin")
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    assert update.update_check_supported() is True
    assert update.update_install_supported() is False


def test_checking_is_pointless_inside_a_flatpak(monkeypatch):
    """The software centre already says so; a second notice is noise."""
    monkeypatch.setenv("FLATPAK_ID", "io.github.littlephish.EveStrait")
    assert update.update_check_supported() is False


def test_auto_check_is_off_where_checking_is_unsupported(monkeypatch):
    monkeypatch.setenv("FLATPAK_ID", "io.github.littlephish.EveStrait")
    assert update.auto_check_enabled() is False


def test_legacy_name_still_means_install(monkeypatch):
    """updates_supported() is referenced elsewhere; keep it meaning install."""
    monkeypatch.setattr(update.sys, "platform", "win32")
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    assert update.updates_supported() is update.update_install_supported()
