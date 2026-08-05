"""Generic QThread worker so slow I/O (SDE download, ESI calls) stays off the UI thread."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QThread, Signal


class Worker(QThread):
    progress = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            # Pass a progress callback if the target accepts one.
            code = getattr(self._fn, "__code__", None)
            if code is not None and "progress" in code.co_varnames:
                self._kwargs.setdefault("progress", self.progress.emit)
            result = self._fn(*self._args, **self._kwargs)
            self.finished_ok.emit(result)
        except Exception as exc:  # surfaced to the user via .failed
            self.failed.emit(f"{type(exc).__name__}: {exc}")
