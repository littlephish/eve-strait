"""What the app is doing in the background, and how to say it in one line.

Deliberately Qt-free so the display rules can be tested without a widget or
an event loop. MainWindow owns one of these and mirrors it into the status
bar; the workers themselves know nothing about it.
"""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QWidget

from .theme import TEXT_MUTED

DEFAULT_LABEL = "Working…"


class TaskRegistry:
    """Running background tasks, in the order they started."""

    def __init__(self):
        # dict preserves insertion order, which is what "oldest first" means.
        self._tasks: dict[object, str] = {}

    def add(self, key, label: str) -> None:
        self._tasks[key] = label or DEFAULT_LABEL

    def update(self, key, label: str) -> None:
        """Replace a running task's label, e.g. from a progress signal.

        Unknown keys are ignored: a worker can emit progress after it has
        finished, and that must not resurrect it into the status bar.
        """
        if key in self._tasks and label:
            self._tasks[key] = label

    def remove(self, key) -> None:
        self._tasks.pop(key, None)      # pop, not del: double removal is fine

    def __len__(self) -> int:
        return len(self._tasks)

    @property
    def active(self) -> bool:
        return bool(self._tasks)

    def summary(self) -> str:
        """One line for the status bar. Empty when nothing is running."""
        if not self._tasks:
            return ""
        labels = list(self._tasks.values())
        if len(labels) == 1:
            return labels[0]
        # Oldest first: a long job holding the line is less distracting than
        # a label that flickers each time a short job starts.
        return f"{labels[0]} (+{len(labels) - 1} more)"

    def tooltip(self) -> str:
        return "\n".join(self._tasks.values())


class BusyIndicator(QWidget):
    """Spinner plus one line of text, for the right-hand end of the status bar.

    Added with addPermanentWidget() rather than addWidget(): showMessage() owns
    the left-hand area and is used all over this app for transient text, so a
    non-permanent widget would be shoved around by it.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.spinner = QProgressBar()
        self.spinner.setRange(0, 0)          # indeterminate
        self.spinner.setTextVisible(False)
        self.spinner.setFixedSize(70, 12)
        row.addWidget(self.spinner)

        self.label = QLabel("")
        self.label.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        row.addWidget(self.label)

        self.hide()

    def set_state(self, summary: str, tooltip: str = "") -> None:
        if not summary:
            self.hide()
            return
        self.label.setText(summary)
        self.setToolTip(tooltip or summary)
        self.show()
