"""The Linux packaging assets have to agree with each other.

Flathub rejects a build whose app ID does not match across the manifest,
the desktop entry, the AppStream metainfo and the icon filename. That is a
trivial mistake to make and a slow one to discover, since it surfaces in a
review queue rather than at build time.

These are structural checks only. `desktop-file-validate` and
`appstreamcli validate` are the real gate and need Linux; see the plan.
"""
import configparser
import xml.etree.ElementTree as ET
from pathlib import Path

LINUX = Path(__file__).resolve().parent.parent / "dist_assets" / "linux"
APP_ID = "io.github.littlephish.EveStrait"


def test_every_asset_is_named_for_the_app_id():
    for suffix in (".desktop", ".metainfo.xml", ".svg"):
        assert (LINUX / f"{APP_ID}{suffix}").is_file(), suffix


def test_metainfo_is_well_formed_and_declares_the_app_id():
    root = ET.parse(LINUX / f"{APP_ID}.metainfo.xml").getroot()
    assert root.tag == "component"
    assert root.get("type") == "desktop-application"
    assert root.findtext("id") == APP_ID


def test_metainfo_launchable_points_at_the_desktop_file():
    root = ET.parse(LINUX / f"{APP_ID}.metainfo.xml").getroot()
    launchable = root.find("launchable")
    assert launchable is not None
    assert launchable.get("type") == "desktop-id"
    assert launchable.text == f"{APP_ID}.desktop"


def test_metainfo_declares_the_licence_the_repo_actually_uses():
    """LICENSE is GPLv3; a wrong SPDX id here is a Flathub review comment."""
    root = ET.parse(LINUX / f"{APP_ID}.metainfo.xml").getroot()
    assert root.findtext("project_license") == "GPL-3.0-or-later"
    assert root.findtext("metadata_license")


def test_metainfo_has_what_flathub_requires():
    root = ET.parse(LINUX / f"{APP_ID}.metainfo.xml").getroot()
    for tag in ("name", "summary", "description", "content_rating"):
        assert root.find(tag) is not None, tag
    assert root.find("screenshots/screenshot/image") is not None


def test_desktop_entry_is_parseable_and_consistent():
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(LINUX / f"{APP_ID}.desktop", encoding="utf-8")
    entry = cp["Desktop Entry"]
    assert entry["Type"] == "Application"
    assert entry["Icon"] == APP_ID
    # Exec must be the console script pyproject installs, or the launcher
    # silently does nothing.
    assert entry["Exec"] == "eve-strait"
    assert entry["Categories"].endswith(";")
    assert entry["Keywords"].endswith(";")


def test_exec_matches_the_declared_console_script():
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml"
            ).read_text(encoding="utf-8")
    assert "eve-strait = " in text
