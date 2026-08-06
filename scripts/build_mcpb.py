"""Package the MCP server as a .mcpb bundle for Claude Desktop.

    uv run python scripts/build_mcpb.py

Produces dist/eve-strait.mcpb, which installs via Claude Desktop's
Settings -> Extensions -> Advanced settings -> install a custom bundle.

A .mcpb is a zip with manifest.json at the root (spec: github.com/anthropics/mcpb).
The bundle has to stand on its own, so it carries the eve_strait package and
the handful of pure-Python runtime deps rather than assuming the user's venv
exists. Only the headless path is needed: the MCP server never imports Qt, so
PySide6 stays out and the bundle is small.
"""
from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "eve_strait"
OUT = ROOT / "dist"
STAGE = OUT / "_mcpb"

# requests and its dependency chain. Pure Python only, so the bundle stays
# portable across interpreters.
#
# charset_normalizer is deliberately NOT vendored: it is mypyc-compiled, and
# its extension module lives at the top level of site-packages rather than
# inside the package, so copying the directory yields a broken import. The
# .pyd is also built per CPython version, which would pin the bundle to one.
# requests only needs it to sniff the encoding of undeclared text; every
# endpoint here returns declared JSON, so the entry point silences the warning.
VENDOR = ["requests", "urllib3", "certifi", "idna"]

ENTRY = '''"""Bundle entry point. Claude Desktop runs this over stdio."""
import os
import sys
import warnings

# charset_normalizer is not vendored (see scripts/build_mcpb.py). requests
# warns about it on import; the ESI and zKillboard endpoints all declare their
# encoding, so the warning is noise on a stream Claude Desktop is reading.
warnings.filterwarnings("ignore", message=".*character detection dependency.*")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from eve_strait.ai.mcp_server import serve

if __name__ == "__main__":
    raise SystemExit(serve())
'''


def _version() -> str:
    import re
    m = re.search(r'__version__\s*=\s*"([^"]+)"',
                  (SRC / "__init__.py").read_text(encoding="utf-8"))
    return m.group(1) if m else "0.0.0"


def _site_packages() -> Path:
    """Where the project's venv keeps its libraries."""
    for base in (ROOT / ".venv" / "Lib" / "site-packages",
                 ROOT / ".venv" / "lib" / "site-packages"):
        if base.is_dir():
            return base
    for p in sys.path:                      # fall back to this interpreter's
        if p.endswith("site-packages") and Path(p).is_dir():
            return Path(p)
    raise SystemExit("Could not locate site-packages; run this with uv run.")


def _manifest(version: str) -> dict:
    return {
        "manifest_version": "0.3",
        "name": "eve-strait",
        "display_name": "Eve-Strait",
        "version": version,
        "description": "EVE Online route and system intelligence for New Eden.",
        "long_description": (
            "Look up solar systems, sovereignty, gate traffic, ratting and "
            "kill activity, jump range and cyno losses from the Eve-Strait "
            "route planner.\n\n"
            "Read-only unless you enable writes in Eve-Strait under "
            "File -> AI assistant. The server refuses to start at all until "
            "you enable it there."),
        "author": {"name": "LittlePhish",
                   "url": "https://github.com/littlephish/eve-strait"},
        "repository": {"type": "git",
                       "url": "https://github.com/littlephish/eve-strait"},
        "license": "GPL-3.0-or-later",
        "keywords": ["eve online", "eve", "routing", "maps", "gaming"],
        "server": {
            "type": "python",
            "entry_point": "server/main.py",
            "mcp_config": {
                "command": "python",
                "args": ["${__dirname}/server/main.py"],
                "env": {"PYTHONPATH": "${__dirname}/server/lib"},
                # Windows ships python as python.exe on PATH; macOS/Linux
                # generally need python3 to avoid a stale Python 2.
                "platform_overrides": {
                    "darwin": {"command": "python3"},
                    "linux": {"command": "python3"},
                },
            },
        },
        "compatibility": {"runtimes": {"python": ">=3.11,<4.0"}},
        # Declared so the install screen shows what it can do before you
        # grant it anything. tools_generated stays false: this is the real list.
        "tools": [{"name": t.name, "description": t.description.split(".")[0] + "."}
                  for t in _servable_tools()],
        "tools_generated": False,
    }


def _servable_tools():
    sys.path.insert(0, str(ROOT / "src"))
    from eve_strait.ai import mcp_server
    from eve_strait.ai import tools
    return [t for t in tools.TOOLS if t.name not in mcp_server._UI_ONLY]


def main() -> int:
    import json

    version = _version()
    if STAGE.exists():
        shutil.rmtree(STAGE)
    lib = STAGE / "server" / "lib"
    lib.mkdir(parents=True)

    # The app itself, minus caches and the Qt-only UI package.
    shutil.copytree(SRC, lib / "eve_strait",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "ui"))
    # ui/ is dropped, but the package imports it lazily only from main(), so
    # the headless entry point never touches it.

    site = _site_packages()
    missing = []
    for name in VENDOR:
        src = site / name
        if src.is_dir():
            shutil.copytree(src, lib / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            missing.append(name)
    if missing:
        print(f"WARNING: not vendored (not found in {site}): {', '.join(missing)}")

    (STAGE / "server" / "main.py").write_text(ENTRY, encoding="utf-8")
    (STAGE / "manifest.json").write_text(
        json.dumps(_manifest(version), indent=2), encoding="utf-8")
    icon = ROOT / "src" / "eve_strait" / "assets" / "icon.png"
    if icon.is_file():
        shutil.copy2(icon, STAGE / "icon.png")
        data = json.loads((STAGE / "manifest.json").read_text(encoding="utf-8"))
        data["icon"] = "icon.png"
        (STAGE / "manifest.json").write_text(json.dumps(data, indent=2),
                                             encoding="utf-8")

    OUT.mkdir(exist_ok=True)
    bundle = OUT / "eve-strait.mcpb"
    bundle.unlink(missing_ok=True)
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(STAGE.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(STAGE).as_posix())
    shutil.rmtree(STAGE, ignore_errors=True)

    size = bundle.stat().st_size / 1048576
    print(f"Built {bundle}  ({size:.1f} MB, version {version})")
    print("Install: Claude Desktop -> Settings -> Extensions -> Advanced "
          "settings -> install a custom bundle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
