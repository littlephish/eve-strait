"""The Flatpak's offline dependency list has to stay usable.

Flathub builds with no network, so python3-requirements.json is committed
rather than generated at build time -- a generation step that needs the
Flatpak SDK cannot run in the build container that is already inside it,
which is how the first Flatpak build failed.

These checks are cheap and catch the ways a regenerated file goes wrong.
"""
import json
from pathlib import Path

LINUX = Path(__file__).resolve().parent.parent / "dist_assets" / "linux"
DEPS = LINUX / "python3-requirements.json"


def _deps():
    return json.loads(DEPS.read_text(encoding="utf-8"))


def test_the_manifest_includes_it():
    manifest = (LINUX / "io.github.littlephish.EveStrait.yml").read_text(
        encoding="utf-8")
    assert "python3-requirements.json" in manifest
    assert DEPS.is_file(), "the manifest includes a file that is not committed"


def test_every_source_is_hashed():
    """An unhashed source is a build that is not reproducible."""
    for s in _deps()["sources"]:
        assert s.get("sha256"), s.get("url")


def test_the_install_is_offline():
    """--no-index is what makes this work on a builder with no network."""
    cmd = " ".join(_deps()["build-commands"])
    assert "--no-index" in cmd
    assert "--find-links" in cmd


def test_nuitka_is_present_and_pinned():
    """The Flatpak compiles, so Nuitka is a build dependency, not a dev tool.

    It ships as an sdist with no wheel, so req2flatpak cannot add it and it
    is appended by hand -- which is exactly the step that gets forgotten.
    """
    deps = _deps()
    urls = " ".join(s["url"] for s in deps["sources"])
    assert "nuitka-" in urls, "Nuitka sdist missing from sources"
    assert "nuitka" in " ".join(deps["build-commands"])


def test_the_nuitka_pin_matches_the_windows_build():
    """One compiler across platforms, or the two builds differ silently."""
    root = LINUX.parent.parent
    urls = " ".join(s["url"] for s in _deps()["sources"])
    version = urls.split("nuitka-")[1].split(".tar.gz")[0]
    for path in (".github/workflows/release.yml", "scripts/build_exe.ps1"):
        text = (root / path).read_text(encoding="utf-8")
        assert f"nuitka=={version}" in text, f"{path} pins a different Nuitka"


def test_keyring_is_included():
    """Tripwire's password needs a credential store in the shipped build."""
    assert "keyring" in " ".join(_deps()["build-commands"])


def test_requirements_source_is_committed():
    """The generated file is only regenerable if its input is kept."""
    assert (LINUX / "requirements.in").is_file()
