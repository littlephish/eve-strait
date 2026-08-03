"""Entry point: ``uv run eve-jump-planner`` or ``uv run python -m eve_jump_planner``."""
from __future__ import annotations

import sys


def main() -> int:
    # Imported lazily so ``--help`` style tooling doesn't need Qt loaded.
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("EVE Jump Planner")
    app.setOrganizationName("eve-jump-planner")

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
