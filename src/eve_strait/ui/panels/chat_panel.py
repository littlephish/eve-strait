"""Chat with the routing assistant.

This panel is only ever built when a provider key is configured, so an install
that never opts in has no AI surface at all.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal

from ..theme import pad
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class _Input(QPlainTextEdit):
    """Enter sends, Shift+Enter makes a newline."""
    submitted = Signal()

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)):
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class ChatPanel(QWidget):
    asked = Signal(str)
    reset_requested = Signal()

    def __init__(self, provider_label: str):
        super().__init__()
        v = pad(QVBoxLayout(self))

        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        v.addWidget(self.transcript, 1)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#888; font-size:11px;")
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        self.input = _Input()
        self.input.setPlaceholderText(
            "Ask about the route, or tell it what you want. Enter sends.")
        self.input.setFixedHeight(64)
        self.input.submitted.connect(self._send)
        v.addWidget(self.input)

        row = QHBoxLayout()
        self.btn_send = QPushButton("Send")
        self.btn_send.clicked.connect(self._send)
        btn_clear = QPushButton("New conversation")
        btn_clear.clicked.connect(self._reset)
        row.addWidget(self.btn_send)
        row.addWidget(btn_clear)
        v.addLayout(row)

        self._say("system", f"Connected to {provider_label}. Everything you "
                            "ask is sent to that provider.")

    # ------------------------------------------------------------------
    def _send(self):
        text = self.input.toPlainText().strip()
        if not text or not self.btn_send.isEnabled():
            return
        self.input.clear()
        self._say("you", text)
        self.set_busy(True)
        self.asked.emit(text)

    def _reset(self):
        self.transcript.clear()
        self.status.setText("")
        self.reset_requested.emit()

    def set_busy(self, busy: bool):
        self.btn_send.setEnabled(not busy)
        self.input.setReadOnly(busy)
        if not busy:
            self.status.setText("")

    def set_status(self, text: str):
        self.status.setText(text)

    def add_reply(self, text: str, tool_log: list | None = None):
        self._say("assistant", text)
        if tool_log:
            self._say("tools", " · ".join(tool_log))
        self.set_busy(False)

    def add_error(self, text: str):
        self._say("error", text)
        self.set_busy(False)

    def _say(self, who: str, text: str):
        colour = {"you": "#8fd130", "assistant": "#cfe3ff", "error": "#ff6b6b",
                  "tools": "#7d8aa0", "system": "#888"}.get(who, "#cfe3ff")
        name = {"you": "You", "assistant": "Assistant", "error": "Error",
                "tools": "ran", "system": ""}.get(who, who)
        body = _escape(text).replace("\n", "<br>")
        label = f"<b style='color:{colour}'>{name}:</b> " if name else ""
        style = "font-size:11px;" if who in ("tools", "system") else ""
        self.transcript.append(
            f"<div style='color:{colour};{style}'>{label}"
            f"<span style='color:#dfe7f5'>{body}</span></div>")
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))
