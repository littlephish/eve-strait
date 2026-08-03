"""Entry point for building a standalone executable (Nuitka/PyInstaller).

Running the app normally still uses ``uv run eve-jump-planner`` (see pyproject).
This module just gives the compiler a concrete top-level script.
"""
from __future__ import annotations

import os
import sys

# When frozen, the package is bundled; in a source checkout, add src/ to path.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from eve_jump_planner.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
