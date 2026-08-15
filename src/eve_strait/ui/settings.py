"""One settings window, instead of eight entries on the File menu.

Every one of these used to be its own modal launched from its own menu item,
which meant that changing two related things -- say the Wanderer instance and
which wormholes to trust -- was two trips through a menu, and that nothing was
discoverable unless you already knew it existed.

The pages are the *existing* dialogs, reparented into tabs rather than
rewritten. Each one already knew how to build itself and how to report what the
user chose; the only thing it does not need any more is its own OK button, so
that gets hidden and the settings window supplies one for all of them. Keeping
them intact means the accessors main_window already calls (``pairs()``,
``names()``, ``values()``) keep working exactly as before.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QMessageBox,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config
from .theme import GAP, GUTTER, INDENT, TEXT_MUTED, WARN, pad


class SettingsDialog(QDialog):
    def __init__(self, parent, start_tab: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(780, 620)
        v = pad(QVBoxLayout(self))

        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)
        self.pages: dict[str, QWidget] = {}

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        # Not wired straight to reject(): Escape and the titlebar X both
        # funnel into reject() too by Qt's own default handling, with no way
        # to tell them apart from a deliberate Cancel click at that point. A
        # flag set only by this button lets reject() warn on the accidental
        # paths and stay silent on the one that is not.
        self.buttons.rejected.connect(self._cancel_clicked)
        v.addWidget(self.buttons)
        self._start_tab = start_tab
        self._explicit_cancel = False

    def _cancel_clicked(self):
        self._explicit_cancel = True
        self.reject()

    def reject(self):
        """Escape and the titlebar X land here too, not just Cancel.

        This dialog used to be eight separate ones, each holding at most one
        setting's worth of unsaved typing. One accidental Escape now risks
        nine tabs of changes at once, silently, which is a materially bigger
        loss than the old design ever risked -- worth one confirmation click
        to avoid. A deliberate Cancel click still just cancels.
        """
        if not self._explicit_cancel:
            choice = QMessageBox.question(
                self, "Discard changes?",
                "Closing without OK discards anything changed on every tab, "
                "not just the one you were looking at.\n\nDiscard?",
                QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if choice != QMessageBox.StandardButton.Discard:
                return
        super().reject()

    def add_page(self, title: str, widget: QWidget, scroll: bool = False):
        """Add a tab. A QDialog is accepted and stripped of its own buttons."""
        for box in widget.findChildren(QDialogButtonBox):
            # The settings window owns OK and Cancel now. Leaving these would
            # give a tab two ways to be accepted, one of which closes nothing.
            box.hide()
        if isinstance(widget, QDialog):
            # Reparenting a QDialog into a layout makes it an ordinary child
            # widget; it stops being a window and never runs its own event
            # loop, but every accessor on it still works.
            widget.setWindowFlags(Qt.WindowType.Widget)
        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.Shape.NoFrame)
            area.setWidget(widget)
            self.tabs.addTab(area, title)
        else:
            self.tabs.addTab(widget, title)
        self.pages[title] = widget
        if self._start_tab and title == self._start_tab:
            self.tabs.setCurrentIndex(self.tabs.count() - 1)
        return widget


class ScopesPage(QWidget):
    """Pick what the app may ask EVE for, with the risk spelled out.

    The feedback that prompted this was that people are relaxed about most
    scopes and specifically wary of the few that amount to live intel, so a
    flat list of eleven near-identical strings is the one thing that cannot
    work. Each entry says what it turns on, what it reads, and -- where there
    is one -- what somebody learns if they see it.
    """

    def __init__(self, parent=None, selected: list[str] | None = None):
        super().__init__(parent)
        v = pad(QVBoxLayout(self))
        self.boxes: dict[str, QCheckBox] = {}
        current = set(selected if selected is not None else config.get_scopes())

        intro = QLabel(
            "Unticking is always safe - the app does without the feature. "
            "<b>Ticking something new</b> also needs that scope on your "
            "application at developers.eveonline.com, or signing in fails "
            "with <code>invalid_scope</code>.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(intro)

        for title, note, scopes in config.SCOPE_GROUPS:
            head = QLabel(title)
            f = head.font()
            f.setBold(True)
            head.setFont(f)
            head.setContentsMargins(0, GAP, 0, 0)
            v.addWidget(head)
            if note:
                lbl = QLabel(note)
                lbl.setWordWrap(True)
                lbl.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
                lbl.setContentsMargins(INDENT, 0, 0, 0)
                v.addWidget(lbl)

            for info in scopes:
                box = QCheckBox(info.title)
                box.setChecked(info.required or info.scope in current)
                if info.required:
                    box.setEnabled(False)
                    box.setToolTip("Required to sign in at all.")
                box.setContentsMargins(INDENT, 0, 0, 0)
                v.addWidget(box)
                self.boxes[info.scope] = box

                detail = QLabel(info.detail)
                detail.setWordWrap(True)
                detail.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
                detail.setContentsMargins(INDENT + GUTTER + 10, 0, 0, 0)
                v.addWidget(detail)

                if info.risk:
                    # Warning colour *and* a glyph and a word: colour on its
                    # own would not survive being printed, screenshotted, or
                    # looked at by someone who does not separate these hues.
                    risk = QLabel(f"⚠  {info.risk}")
                    risk.setWordWrap(True)
                    risk.setStyleSheet(f"color:{WARN}; font-size:11px;")
                    risk.setContentsMargins(INDENT + GUTTER + 10, 0, 0, GAP)
                    v.addWidget(risk)

        v.addStretch(1)

    def scopes(self) -> list[str]:
        """In catalogue order, so the list is stable between saves."""
        return [s.scope for s in config.scope_catalogue()
                if self.boxes[s.scope].isChecked()]

    def set_scopes(self, scopes: list[str]):
        wanted = set(scopes)
        for scope, box in self.boxes.items():
            if box.isEnabled():
                box.setChecked(scope in wanted)


class AppearancePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        from .theme import get_chrome

        v = pad(QVBoxLayout(self))
        self.chk_native = QCheckBox("Use native window chrome")
        self.chk_native.setChecked(get_chrome() == "native")
        v.addWidget(self.chk_native)

        why = QLabel(
            "The dark theme overrides whatever you have set at the operating "
            "system level, including high contrast and forced colours. If you "
            "rely on those, turn this on and the panels will follow them "
            "instead.\n\nThe map keeps its own colours either way - it is a "
            "chart rather than chrome.\n\nTakes effect when you restart.")
        why.setWordWrap(True)
        why.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        v.addWidget(why)
        v.addStretch(1)

    def native(self) -> bool:
        return self.chk_native.isChecked()
