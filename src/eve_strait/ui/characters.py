"""Manage every linked character in one window.

The character panel carries a combo box, which is the right shape for the one
thing it is for: switching the active character while you work. It is the wrong
shape for everything else. Two dozen linked alts behind a drop-down cannot tell
you which ones still have usable data, which are missing a scope a feature
needs, or which you stopped using months ago -- and "Unlink" sitting next to a
combo box acts on whatever happens to be selected.

**No OK or Cancel.** Every action here takes effect immediately, so there is
nothing to stage and nothing to discard. That is why this is its own window
rather than a tab in the settings dialog: adding a character opens an OAuth
round trip that writes the token store from a browser callback, and unlinking
one removes a refresh token. Neither can be rolled back by a Cancel button, and
a dialog that promises to discard changes should not contain actions that
ignore it.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .. import config
from ..esi import client as _client
from .theme import TEXT_MUTED, WARN, pad

# Past this, the cached asset list is old enough that acting on it is a
# mistake waiting to happen -- a structure you no longer have docking rights
# at, or a station you moved out of weeks ago.
STALE_DAYS = 7


def describe_age(epoch: float | None) -> str:
    """"3 days ago" / "just now", or a plain never."""
    if not epoch:
        return "never"
    delta = time.time() - epoch
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 172800:
        return f"{int(delta // 3600)} h ago"
    return f"{int(delta // 86400)} days ago"


class CharacterManager(QDialog):
    """One row per linked character. Modeless; actions apply at once."""

    login_requested = Signal()
    unlink_requested = Signal(int)
    activate_requested = Signal(int)
    reload_requested = Signal(object)      # list[int] of character ids

    _COLUMNS = ("Character", "Active", "Scopes", "Locations", "Assets read")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage characters")
        # A window rather than a dialog: it is meant to stay open beside the
        # map while you work through a list, not to be answered and dismissed.
        self.setWindowFlags(Qt.WindowType.Window)
        self.resize(720, 460)
        v = pad(QVBoxLayout(self))

        self.table = QTableWidget(0, len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        # Several may be selected so a stale batch can be refreshed in one go.
        # Unlink and Make active stay single-selection actions -- see
        # _on_selection: a button that reads as applying to one row while
        # acting on four is the same trap the route panel's dock picker avoids.
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.doubleClicked.connect(self._activate_selected)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, len(self._COLUMNS)):
            head.setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.table, 1)

        self.lbl_note = QLabel("")
        self.lbl_note.setWordWrap(True)
        self.lbl_note.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        v.addWidget(self.lbl_note)

        row = QHBoxLayout()
        self.btn_add = QPushButton("Add another…")
        self.btn_add.setToolTip(
            "Log in another character. Opens your browser; the new character "
            "is linked as soon as EVE calls back.")
        self.btn_add.clicked.connect(self.login_requested)
        row.addWidget(self.btn_add)

        self.btn_active = QPushButton("Make active")
        self.btn_active.setToolTip(
            "Use this character for assets, standings, starbases and "
            "location. Double-clicking a row does the same.")
        self.btn_active.clicked.connect(self._activate_selected)
        row.addWidget(self.btn_active)

        self.btn_reload = QPushButton("Reload assets")
        self.btn_reload.setToolTip(
            "Re-read the selected characters' dockable locations from EVE.")
        self.btn_reload.clicked.connect(self._reload_selected)
        row.addWidget(self.btn_reload)

        self.btn_unlink = QPushButton("Unlink")
        self.btn_unlink.setToolTip(
            "Remove this character from the app. The cached asset list is "
            "kept, so re-adding them later costs one login and no refetch.")
        self.btn_unlink.clicked.connect(self._unlink_selected)
        row.addWidget(self.btn_unlink)

        row.addStretch(1)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_close)
        v.addLayout(row)

        self._rows: list[int] = []      # character_id per table row
        self._on_selection()

    # -- population ---------------------------------------------------------
    def refresh(self, tokens: dict, active_id: int | None):
        """Rebuild from {character_id: Token} and whichever is active."""
        wanted = set(config.get_scopes())
        keep = self.selected_ids()

        self.table.setRowCount(0)
        self._rows = []
        stale = 0
        gaps = 0

        for cid, tok in sorted(
                tokens.items(),
                key=lambda kv: (getattr(kv[1], "character_name", "") or "").lower()):
            count, mtime = _client.dockables_cache_info(cid)
            granted = set(getattr(tok, "scopes", None) or [])
            missing = wanted - granted

            r = self.table.rowCount()
            self.table.insertRow(r)
            self._rows.append(cid)

            name = QTableWidgetItem(
                getattr(tok, "character_name", None) or str(cid))
            name.setToolTip(f"Character ID {cid}")
            self.table.setItem(r, 0, name)

            # A dot, not a checkbox: it is a state of the app, not something
            # you toggle per row. Make active is the verb.
            self.table.setItem(r, 1, self._cell(
                "●" if cid == active_id else "", centre=True))

            scopes = self._cell(
                f"{len(granted)}" if not missing
                else f"{len(granted)} · {len(missing)} missing",
                warn=bool(missing), centre=True)
            if missing:
                gaps += 1
                scopes.setToolTip(
                    "Not granted at this character's last login:\n  "
                    + "\n  ".join(sorted(missing))
                    + "\n\nFeatures needing these stay dark for this "
                      "character. Re-add them to grant it.")
            self.table.setItem(r, 2, scopes)

            self.table.setItem(r, 3, self._cell(
                "—" if count is None else str(count), centre=True))

            old = mtime is not None and (
                time.time() - mtime) > STALE_DAYS * 86400
            stale += 1 if (old or mtime is None) else 0
            read = self._cell(describe_age(mtime), warn=old or mtime is None)
            if mtime:
                read.setToolTip(time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(mtime)))
            elif count is None:
                read.setToolTip(
                    "Never loaded. This character contributes no dockable "
                    "locations to routing until you reload their assets.")
            self.table.setItem(r, 4, read)

        self._restore(keep)
        self._note(len(tokens), stale, gaps)
        self._on_selection()

    @staticmethod
    def _cell(text: str, *, warn: bool = False, centre: bool = False):
        item = QTableWidgetItem(text)
        if centre:
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if warn:
            from PySide6.QtGui import QColor
            item.setForeground(QColor(WARN))
        return item

    def _note(self, total: int, stale: int, gaps: int):
        bits = [f"{total} character{'s' if total != 1 else ''} linked"]
        if stale:
            bits.append(f"{stale} with asset data older than {STALE_DAYS} "
                        f"days or never read")
        if gaps:
            bits.append(f"{gaps} missing a scope the app now asks for")
        self.lbl_note.setText(" · ".join(bits))

    def _restore(self, ids: list[int]):
        if not ids:
            return
        self.table.clearSelection()
        for r, cid in enumerate(self._rows):
            if cid in ids:
                self.table.selectRow(r)

    # -- selection ----------------------------------------------------------
    def selected_ids(self) -> list[int]:
        rows = {i.row() for i in self.table.selectedIndexes()}
        return [self._rows[r] for r in sorted(rows) if r < len(self._rows)]

    def _on_selection(self):
        n = len(self.selected_ids())
        # Both of these name one character in their confirmation text, so
        # offering them for four would be lying about what is about to happen.
        self.btn_active.setEnabled(n == 1)
        self.btn_unlink.setEnabled(n == 1)
        self.btn_reload.setEnabled(n >= 1)

    # -- actions ------------------------------------------------------------
    def _activate_selected(self):
        ids = self.selected_ids()
        if len(ids) == 1:
            self.activate_requested.emit(ids[0])

    def _unlink_selected(self):
        ids = self.selected_ids()
        if len(ids) == 1:
            self.unlink_requested.emit(ids[0])

    def _reload_selected(self):
        ids = self.selected_ids()
        if ids:
            self.reload_requested.emit(ids)
