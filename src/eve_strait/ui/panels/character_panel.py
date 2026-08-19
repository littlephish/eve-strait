"""Character login + the character's dockable stations/structures."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...data import docking
from ...data.ships import Ship
from ..collapsible import Section
from ..theme import GUTTER, TEXT_MUTED, pad

_ROLE_SYS = Qt.ItemDataRole.UserRole
_STATUS_ICON = {"ok": "✓", "risky": "⚠", "no docking": "✗"}


class CharacterPanel(QWidget):
    login_requested = Signal()
    load_structures_requested = Signal()
    load_all_structures_requested = Signal()
    add_system = Signal(int)
    character_changed = Signal(int)          # character_id
    unlink_requested = Signal(int)           # character_id
    goto_location_requested = Signal()
    scopes_requested = Signal()
    scan_cyno_requested = Signal()
    force_cyno_requested = Signal()

    def __init__(self):
        super().__init__()
        # No gutter here: the stack fills the panel and each page applies its
        # own, so the two never double up when switching.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # Signed out and signed in are different jobs, so they get different
        # panels rather than one panel with half its controls greyed out. The
        # old layout showed an empty character combo above four buttons that
        # could not do anything yet, which told a new user nothing about what
        # signing in would give them.
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.stack.addWidget(self._build_signed_out())
        self.stack.addWidget(self._build_signed_in())

    # -- signed out ---------------------------------------------------------
    def _build_signed_out(self) -> QWidget:
        page = QWidget()
        v = pad(QVBoxLayout(page))
        v.addStretch(1)

        title = QLabel("Not signed in")
        f = title.font()
        f.setPointSize(f.pointSize() + 3)
        f.setBold(True)
        title.setFont(f)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)

        # Say what it buys before asking for it.
        blurb = QLabel(
            "Sign in with EVE to unlock:"
            "<ul style='margin-left:-20px'>"
            "<li>your dockable structures and assets</li>"
            "<li>where your characters are right now</li>"
            "<li>standings, to route around hostiles</li>"
            "<li>set destination in the game client</li>"
            "</ul>")
        blurb.setTextFormat(Qt.TextFormat.RichText)
        blurb.setWordWrap(True)
        v.addWidget(blurb)

        self.btn_login = QPushButton("Sign in with EVE Online")
        self.btn_login.setObjectName("primary")
        self.btn_login.setMinimumHeight(38)
        bf = self.btn_login.font()
        bf.setBold(True)
        self.btn_login.setFont(bf)
        self.btn_login.clicked.connect(self.login_requested)
        v.addWidget(self.btn_login)

        self.btn_scopes = QPushButton("Choose what to share…")
        self.btn_scopes.setFlat(True)
        self.btn_scopes.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_scopes.clicked.connect(self.scopes_requested)
        v.addWidget(self.btn_scopes)

        # Status line during the SSO round trip. MainWindow writes here.
        self.lbl_login = QLabel("")
        self.lbl_login.setWordWrap(True)
        self.lbl_login.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.lbl_login)

        foot = QLabel("The map and route planner work without this, "
                      "using public data only.")
        foot.setWordWrap(True)
        foot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        foot.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        v.addWidget(foot)
        v.addStretch(2)
        return page

    # -- signed in ----------------------------------------------------------
    def _build_signed_in(self) -> QWidget:
        page = QWidget()
        # Was 0 all round, which put every label hard against the dock border.
        v = pad(QVBoxLayout(page))

        self.lbl_active = QLabel("")
        self.lbl_active.setWordWrap(True)
        v.addWidget(self.lbl_active)

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
        self.btn_add = QPushButton("Add another…")
        self.btn_add.clicked.connect(self.login_requested)
        btn_row.addWidget(self.btn_add)
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

        # Bulk load is a context action, not the main click: it walks
        # every linked character, so it should be chosen rather than
        # stumbled into.
        self.btn_structs.setContextMenuPolicy(
            Qt.ContextMenuPolicy.ActionsContextMenu)
        act_all = QAction("Load for all linked characters", self.btn_structs)
        act_all.triggered.connect(self.load_all_structures_requested)
        self.btn_structs.addAction(act_all)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)   # indeterminate spinner
        self.progress.setTextVisible(True)
        self.progress.hide()
        v.addWidget(self.progress)

        v.addWidget(QLabel("Docked/asset locations (double-click to add):"))
        self.struct_list = QListWidget()
        self.struct_list.itemDoubleClicked.connect(self._on_double)
        # Stretches instead of a fixed 220px cap: this is the one thing in the
        # panel worth reading, so it should take the space the panel has.
        v.addWidget(self.struct_list, 1)

        # -- cyno alts ------------------------------------------------------
        # Sits with the characters because that is what it is: a roll-call of
        # which of your own alts is parked in something that can light you in.
        self.sec_cyno = Section("Cyno alts")
        self.btn_cyno = QPushButton("Scan my characters")
        self.btn_cyno.setToolTip(
            "Checks every linked character for a cynosural field generator "
            "fitted to the ship they are currently in.")
        self.btn_cyno.clicked.connect(self.scan_cyno_requested)
        self.sec_cyno.add(self.btn_cyno)

        # Asset data is cached for as long as ESI says it is valid (about
        # an hour). Saying so turns "the button did nothing" into "the
        # button correctly did nothing", which is the difference between a
        # bug report and an informed user.
        self.lbl_cyno_age = QLabel("")
        self.lbl_cyno_age.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        self.sec_cyno.add(self.lbl_cyno_age)

        # Force refresh is a menu action, not a second button: it spends
        # the rate-limit budget the cache exists to protect, so it should
        # take deliberate effort to reach.
        self.btn_cyno.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        act_force = QAction("Force refresh (ignores cache)", self.btn_cyno)
        act_force.triggered.connect(self.force_cyno_requested)
        self.btn_cyno.addAction(act_force)
        self.cyno_list = QListWidget()
        self.cyno_list.setToolTip("Double-click to add that system as a waypoint.")
        self.cyno_list.itemDoubleClicked.connect(self._on_double)
        self.sec_cyno.add(self.cyno_list)
        self.lbl_cyno = QLabel("")
        self.lbl_cyno.setWordWrap(True)
        self.lbl_cyno.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        self.sec_cyno.add(self.lbl_cyno)
        v.addWidget(self.sec_cyno)

        self._dockables: list = []
        self._filter = 0  # 0 none, 1 any dock, 2 safe only
        return page

    # -- cyno alts ----------------------------------------------------------
    def set_cyno_freshness(self, fetched_at, expires_at):
        """Show when the cached asset data was read and when it expires."""
        if not fetched_at:
            self.lbl_cyno_age.setText("")
            return
        import time
        when = time.strftime("%H:%M", time.localtime(fetched_at))
        remaining = (expires_at or 0) - time.time()
        if remaining > 60:
            self.lbl_cyno_age.setText(
                f"as of {when} · refreshes in {int(remaining // 60)} min")
        else:
            self.lbl_cyno_age.setText(f"as of {when} · ready to refresh")

    def set_cyno_scanning(self, busy: bool):
        self.btn_cyno.setEnabled(not busy)
        self.btn_cyno.setText("Scanning…" if busy else "Scan my characters")

    def set_cyno_alts(self, alts, notes, system_name_of):
        """alts: [CynoAlt]; notes: reasons a character could not be checked."""
        self.cyno_list.clear()
        for a in sorted(alts, key=lambda x: x.character_name):
            it = QListWidgetItem(
                f"{a.character_name} - {system_name_of(a.system_id)}"
                f"  ({a.summary()})")
            it.setData(_ROLE_SYS, a.system_id)
            self.cyno_list.addItem(it)
        if not alts:
            self.cyno_list.addItem("No linked character is in a cyno-fitted ship.")

        # The caveats belong next to the answer, not in a manual. Assets are
        # cached by ESI for about an hour, so this can lag reality, and a
        # fitted cyno is not the same thing as a lit one.
        bits = [f"{len(alts)} found." if alts else ""]
        bits.append("Fitted modules come from your asset list, which ESI "
                    "caches for about an hour, so a cyno fitted just now may "
                    "not show yet. Being in the ship is not the same as "
                    "having the cyno lit.")
        bits.extend(notes)
        self.sec_cyno.set_summary(f"{len(alts)} ready" if alts else "none found")
        self.lbl_cyno.setText(" ".join(b for b in bits if b))

    # -- state --------------------------------------------------------------
    def set_login(self, name: str | None):
        """Swap between the sign-in pitch and the signed-in controls."""
        self.stack.setCurrentIndex(1 if name else 0)
        if name:
            self.lbl_active.setText(f"Active: <b>{name}</b>")
            self.lbl_login.setText("")     # clear any stale SSO status
        else:
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
