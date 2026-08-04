"""Character login + the character's dockable stations/structures."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...data import docking
from ...data.ships import Ship

_ROLE_SYS = Qt.ItemDataRole.UserRole
_STATUS_ICON = {"ok": "✓", "risky": "⚠", "no docking": "✗"}


class CharacterPanel(QWidget):
    login_requested = Signal()
    load_structures_requested = Signal()
    add_system = Signal(int)

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)

        self.lbl_login = QLabel("-")
        v.addWidget(self.lbl_login)

        self.btn_login = QPushButton("Log in with EVE")
        self.btn_login.clicked.connect(self.login_requested)
        v.addWidget(self.btn_login)

        self.btn_structs = QPushButton("Load my dockable structures")
        self.btn_structs.clicked.connect(self.load_structures_requested)
        v.addWidget(self.btn_structs)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)   # indeterminate spinner
        self.progress.setTextVisible(True)
        self.progress.hide()
        v.addWidget(self.progress)

        v.addWidget(QLabel("Docked/asset locations (double-click to add):"))
        self.struct_list = QListWidget()
        self.struct_list.setMaximumHeight(220)   # keep the list compact
        self.struct_list.itemDoubleClicked.connect(self._on_double)
        v.addWidget(self.struct_list)

        self._dockables: list = []
        self._filter = 0  # 0 none, 1 any dock, 2 safe only

    # -- state --------------------------------------------------------------
    def set_login(self, name: str | None):
        if name:
            self.lbl_login.setText(f"Logged in: <b>{name}</b>")
            self.btn_login.setText("Re-authenticate")
        else:
            self.lbl_login.setText("Not logged in.")
            self.btn_login.setText("Log in with EVE")

    def set_loading(self, loading: bool, text: str = ""):
        self.btn_structs.setEnabled(not loading)
        if loading:
            self.progress.setFormat(text or "Loading…")
            self.progress.show()
        else:
            self.progress.hide()

    def set_dockables(self, dockables: list):
        self._dockables = dockables

    def set_filter(self, policy: int):
        self._filter = policy

    def _on_double(self, item: QListWidgetItem):
        sid = item.data(_ROLE_SYS)
        if sid:
            self.add_system.emit(int(sid))

    def render(self, ship: Ship, type_name_of):
        """type_name_of: callable(type_id)->str for NPC station kickout names."""
        self.struct_list.clear()
        if not self._dockables:
            self.struct_list.addItem("No stations/structures found in your assets.")
            return
        shown = 0
        for d in self._dockables:
            if d.kind == "station":
                chk = docking.check_npc_station(ship, type_name_of(d.type_id), d.max_volume)
            else:
                chk = docking.check_structure(ship, d.type_id, d.name, d.location_id)
            if self._filter == 1 and not chk.can_dock:
                continue
            if self._filter == 2 and not (chk.can_dock and chk.safe):
                continue
            icon = _STATUS_ICON[chk.status]
            it = QListWidgetItem(f"{icon}  {d.name}  [{d.kind}] - {chk.note}")
            it.setData(_ROLE_SYS, d.solar_system_id)
            self.struct_list.addItem(it)
            shown += 1
        if shown == 0:
            self.struct_list.addItem("(no locations match the docking filter)")
