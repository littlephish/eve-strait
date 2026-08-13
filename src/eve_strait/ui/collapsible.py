"""A disclosure section whose header still tells you what is inside.

Collapsing controls normally means hiding their state too, which trades one
problem for a worse one: you can no longer tell at a glance whether you are
avoiding incursions without opening the section. So the header carries a
summary line that the owner keeps up to date, and the section stays honest
while closed.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics
from .theme import GAP, INDENT, TIGHT
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class _Summary(QLabel):
    """A label that gives up width instead of taking it from the panel.

    A plain QLabel reports the full width of its text as its *minimum*, so a
    summary like "Anshar - balanced - gates on" set a 462px floor on the whole
    route panel and pushed the controls to its right off the dock. This one
    asks for nothing and elides what will not fit, which is the right trade for
    a line that is already a summary.
    """

    def __init__(self):
        super().__init__("")
        self._full = ""

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def setText(self, text: str):          # noqa: N802 - Qt override
        self._full = text
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        fm = QFontMetrics(self.font())
        shown = fm.elidedText(self._full, Qt.TextElideMode.ElideRight,
                              max(0, self.width()))
        super().setText(shown)
        # The full line is still readable, just not at this width.
        self.setToolTip(self._full if shown != self._full else "")


class Section(QWidget):
    toggled = Signal(bool)

    def __init__(self, title: str, expanded: bool = False):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, TIGHT // 2, 0, TIGHT // 2)
        v.setSpacing(TIGHT // 2)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        self.button = QToolButton()
        self.button.setText(title)
        self.button.setCheckable(True)
        self.button.setChecked(expanded)
        self.button.setAutoRaise(True)
        self.button.setArrowType(Qt.ArrowType.DownArrow if expanded
                                 else Qt.ArrowType.RightArrow)
        self.button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.clicked.connect(self._on_click)
        head.addWidget(self.button)

        self.summary = _Summary()
        self.summary.setStyleSheet("color:#8b97a8;")
        self.summary.setAlignment(Qt.AlignmentFlag.AlignRight
                                  | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.summary, 1)
        v.addLayout(head)

        self.body = QFrame()
        self.body.setVisible(expanded)
        self._body_layout = QVBoxLayout(self.body)
        self._body_layout.setSpacing(GAP)
        self._body_layout.setContentsMargins(INDENT, 0, 0, TIGHT)
        v.addWidget(self.body)

    def add(self, item):
        """Add a widget or a layout to the section body."""
        if isinstance(item, QWidget):
            self._body_layout.addWidget(item)
        else:
            self._body_layout.addLayout(item)

    def set_summary(self, text: str):
        """The one-line state shown while collapsed."""
        self.summary.setText(text)

    def set_expanded(self, on: bool):
        self.button.setChecked(on)
        self._on_click()

    def is_expanded(self) -> bool:
        return self.button.isChecked()

    def _on_click(self):
        on = self.button.isChecked()
        self.button.setArrowType(Qt.ArrowType.DownArrow if on
                                 else Qt.ArrowType.RightArrow)
        self.body.setVisible(on)
        self.toggled.emit(on)
