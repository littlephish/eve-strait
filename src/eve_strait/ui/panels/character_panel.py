"""Character login + the character's dockable stations/structures."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
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
    character_changed = Signal(int)          # character_id
    unlink_requested = Signal(int)           # character_id
    goto_location_requested = Signal()

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)

        self.lbl_login = QLabel("-")
        v.addWidget(self.lbl_login)

        # Character switcher: the active character drives assets, standings,
        # starbases and location.
        char_row = QHBoxLayout()
        char_row.addWidget(QLabel("Character:"))
        self.cmb_char = QComboBox()
        self.cmb_char.currentIndexChanged.connect(self._on_char_picked)
        char_row.addWidget(self.cmb_char, 1)
        v.addLayout(char_row)

        self.lbl_location = QLabel("")
        self.lbl_location.setWordWrap(True)
        v.addWidget(self.lbl_location)

        btn_row = QHBoxLayout()
        self.btn_login = QPushButton("Add character")
        self.btn_login.clicked.connect(self.login_requested)
        btn_row.addWidget(self.btn_login)
        self.btn_unlink = QPushButton("Unlink")
        self.btn_unlink.setToolTip("Remove the selected character from this app.")
        self.btn_unlink.clicked.connect(self._on_unlink)
        btn_row.addWidget(self.btn_unlink)
        v.addLayout(btn_row)

        self.btn_here = QPushButton("Use current location as origin")
        self.btn_here.clicked.connect(self.goto_location_requested)
        v.addWidget(self.btn_here)

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
            self.lbl_login.setText(f"Active: <b>{name}</b>")
        else:
            self.lbl_login.setText("No characters linked.")
            self.lbl_location.setText("")
        self.btn_unlink.setEnabled(bool(name))
        self.btn_here.setEnabled(bool(name))

    def set_characters(self, characters: list[tuple[int, str]], active_id: int | None):
        """characters: [(character_id, name)] with the active one selected."""
        self.cmb_char.blockSignals(True)
        self.cmb_char.clear()
        for cid, name in characters:
            self.cmb_char.addItem(name, cid)
        if active_id is not None:
            idx = self.cmb_char.findData(active_id)
            if idx >= 0:
                self.cmb_char.setCurrentIndex(idx)
        self.cmb_char.setEnabled(len(characters) > 1)
        self.cmb_char.blockSignals(False)

    def current_character_id(self):
        return self.cmb_char.currentData()

    def set_location(self, text: str):
        self.lbl_location.setText(text)

    def _on_char_picked(self, _idx):
        cid = self.cmb_char.currentData()
        if cid is not None:
            self.character_changed.emit(int(cid))

    def _on_unlink(self):
        cid = self.cmb_char.currentData()
        if cid is not None:
            self.unlink_requested.emit(int(cid))

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

    def render(self, ship: Ship, type_name_of, has_rights=None):
        """type_name_of: callable(type_id)->str for NPC station kickout names.
        has_rights: callable(owner_id)->bool for configured docking rights."""
        self.struct_list.clear()
        if not self._dockables:
            self.struct_list.addItem("No stations/structures found in your assets.")
            return
        shown = 0
        for d in self._dockables:
            if d.kind == "station":
                chk = docking.check_npc_station(ship, type_name_of(d.type_id), d.max_volume)
            else:
                chk = docking.check_structure_tether(ship, d.type_id, d.name,
                                                     d.location_id)
                if has_rights and has_rights(getattr(d, "owner_id", 0)):
                    chk = docking.DockCheck(chk.can_dock, True,
                                            f"{chk.note} · docking rights")
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
