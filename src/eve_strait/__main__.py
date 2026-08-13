"""Entry point: ``uv run eve-strait`` or ``uv run python -m eve_strait``."""
from __future__ import annotations

import sys
import traceback


def _log_path():
    from .config import DATA_DIR
    return DATA_DIR / "startup-log.txt"


def _log(message: str) -> None:
    """Append to a log beside the settings.

    The packaged build is compiled with the console disabled, so an exception
    on startup would otherwise vanish and look like "it just closes".
    """
    try:
        import datetime
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} "
                     f"{message}\n")
    except OSError:
        pass


def app_icon():
    """The window / taskbar icon, or None if it is missing.

    Windows takes the *executable's* icon for the taskbar button only once the
    app sets a matching window icon, so this is not redundant with the icon
    compiled into the exe. Missing art must never stop the app starting.
    """
    from pathlib import Path

    from PySide6.QtGui import QIcon

    png = Path(__file__).resolve().parent / "assets" / "icon.png"
    return QIcon(str(png)) if png.is_file() else None


def main() -> int:
    # The MCP server is a headless stdio process: no Qt, no window. It must be
    # handled before QApplication exists, since Claude Desktop spawns it with
    # stdin/stdout wired to the protocol.
    if "--mcp" in sys.argv[1:]:
        from .ai.mcp_server import serve
        return serve()

    # Imported lazily so ``--help`` style tooling doesn't need Qt loaded.
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .ui.main_window import MainWindow

    _log("starting")
    app = QApplication(sys.argv)
    app.setApplicationName("Eve-Strait")
    app.setOrganizationName("eve-strait")
    # Before the window exists, so nothing paints in the stock light theme
    # first and then flips.
    from .ui.theme import apply_theme
    apply_theme(app)
    icon = app_icon()
    if icon is not None:
        app.setWindowIcon(icon)
    else:
        _log("icon.png missing; running without a window icon")
    # Closing a floating panel must not be able to end the session; only
    # quitting explicitly should.
    app.setQuitOnLastWindowClosed(True)

    try:
        window = MainWindow()
        window.show()
    except Exception:
        detail = traceback.format_exc()
        _log("startup failed:\n" + detail)
        QMessageBox.critical(None, "Eve-Strait failed to start",
                             f"{detail}\n\nAlso written to {_log_path()}")
        return 1

    _log("window shown; entering event loop")
    code = app.exec()
    _log(f"event loop returned {code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
