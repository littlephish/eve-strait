"""Read or stamp the app version.

There are three places a version has to agree, and they used to be maintained
by hand:

* ``src/eve_strait/__init__.py`` - what the running app reports, and what the
  updater compares against the latest release tag
* ``pyproject.toml`` - the wheel metadata
* the Nuitka ``--file-version`` / installer ``AppVersion`` - Windows file
  properties and Add/Remove Programs

They drifted. A build from tag v0.2.0 still reported 0.1.0, so the updater saw
0.1.0 < 0.2.0 and offered an "update" to the build that was already installed.

CI stamps the tag in before compiling; the local build script just reads.

    python scripts/version.py              # print the current version
    python scripts/version.py 0.2.0        # stamp it everywhere
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "src" / "eve_strait" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"

_INIT_RE = re.compile(r'^(__version__\s*=\s*")([^"]*)(")', re.M)
_PYPROJECT_RE = re.compile(r'^(version\s*=\s*")([^"]*)(")', re.M)


def read() -> str:
    m = _INIT_RE.search(INIT.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"no __version__ found in {INIT}")
    return m.group(2)


def _sub(path: Path, pattern: re.Pattern, version: str) -> None:
    text = path.read_text(encoding="utf-8")
    new, n = pattern.subn(lambda m: m.group(1) + version + m.group(3), text,
                          count=1)
    if n != 1:
        raise SystemExit(f"could not stamp a version into {path}")
    path.write_text(new, encoding="utf-8")


def stamp(version: str) -> str:
    version = version.strip().lstrip("vV")
    if not re.fullmatch(r"\d+(\.\d+)*([-.+][0-9A-Za-z.-]+)?", version):
        raise SystemExit(f"refusing to stamp implausible version {version!r}")
    _sub(INIT, _INIT_RE, version)
    _sub(PYPROJECT, _PYPROJECT_RE, version)
    return version


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(stamp(sys.argv[1]))
    else:
        print(read())
