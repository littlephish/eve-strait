"""Route building: search, waypoints, dock picker, options and results."""
from __future__ import annotations

from itertools import count

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models import DockOption, Waypoint, docks_for_system, effective_dock

_ROLE_SYS = Qt.ItemDataRole.UserRole
_ROLE_UID = Qt.ItemDataRole.UserRole + 1
_STATUS_ICON = {True: "✓", False: "✗"}


class RoutePanel(QWidget):
    changed = Signal()
    autoroute_requested = Signal()

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.waypoints: list[Waypoint] = []
        self.route_modes: list[str] = []
        self._uid = count(1)
        self._uid_map: dict[int, Waypoint] = {}

        v = QVBoxLayout(self)

        # -- search ---------------------------------------------------------
        sbox = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find system by name...")
        self.search.returnPressed.connect(self._do_search)
        b_find = QPushButton("Find")
        b_find.clicked.connect(self._do_search)
        sbox.addWidget(self.search)
        sbox.addWidget(b_find)
        v.addLayout(sbox)

        self.search_results = QListWidget()
        self.search_results.setMaximumHeight(84)
        self.search_results.itemDoubleClicked.connect(
            lambda it: self._add_system(it.data(_ROLE_SYS)))
        self.search_results.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_results.customContextMenuRequested.connect(self._search_menu)
        v.addWidget(self.search_results)

        hint = QLabel("Double-click a result or click the map to add a waypoint. "
                      "Drag to reorder; right-click to remove. First = origin.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888")
        v.addWidget(hint)

        # -- waypoints ------------------------------------------------------
        v.addWidget(QLabel("Waypoints"))
        self.wp_list = QListWidget()
        self.wp_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.wp_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.wp_list.customContextMenuRequested.connect(self._wp_menu)
        self.wp_list.itemSelectionChanged.connect(self._on_select)
        self.wp_list.model().rowsMoved.connect(self._on_rows_moved)
        v.addWidget(self.wp_list)

        row = QHBoxLayout()
        for text, slot in (("↑", self._up), ("↓", self._down),
                           ("Remove", self._remove_selected), ("Clear", self._clear)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        v.addLayout(row)

        # -- dock picker (change which dock) --------------------------------
        self.lbl_dock = QLabel("Dock:")
        self.cmb_pick = QComboBox()
        self.cmb_pick.currentIndexChanged.connect(self._on_pick)
        pick_row = QHBoxLayout()
        pick_row.addWidget(self.lbl_dock)
        pick_row.addWidget(self.cmb_pick, 1)
        v.addLayout(pick_row)

        # -- options --------------------------------------------------------
        opt = QHBoxLayout()
        opt.addWidget(QLabel("Docking:"))
        self.cmb_policy = QComboBox()
        self.cmb_policy.addItems(
            ["No docking filter", "Require any docking", "Prefer safe docking only"])
        self.cmb_policy.currentIndexChanged.connect(self._emit_changed)
        opt.addWidget(self.cmb_policy, 1)
        v.addLayout(opt)

        opt2 = QHBoxLayout()
        self.cmb_min = QComboBox()
        self.cmb_min.addItems(
            ["Only jumps", "Prefer jumping", "Prefer gating (save fuel/fatigue)"])
        self.cmb_min.setCurrentIndex(1)
        self.cmb_min.currentIndexChanged.connect(self._emit_changed)
        opt2.addWidget(QLabel("Travel:"))
        opt2.addWidget(self.cmb_min, 1)
        v.addLayout(opt2)

        opt3 = QHBoxLayout()
        self.cmb_gate = QComboBox()
        self.cmb_gate.addItems(["Fastest", "Safer (prefer high-sec)",
                                "Less secure (prefer low/null)"])
        self.cmb_gate.currentIndexChanged.connect(self._emit_changed)
        self.b_auto = QPushButton("Auto-route origin → last")
        self.b_auto.clicked.connect(self.autoroute_requested)
        opt3.addWidget(QLabel("Gates:"))
        opt3.addWidget(self.cmb_gate, 1)
        opt3.addWidget(self.b_auto)
        v.addLayout(opt3)

        self.busy = QProgressBar()
        self.busy.setRange(0, 0)          # indeterminate spinner
        self.busy.setFormat("Finding route…")
        self.busy.hide()
        v.addWidget(self.busy)

        self.chk_reactivation = QCheckBox(
            "Minimize reactivation timer (wait out fatigue between jumps)")
        self.chk_reactivation.toggled.connect(self._emit_changed)
        v.addWidget(self.chk_reactivation)

        self.chk_hostile = QCheckBox(
            "Exclude hostile-owned structures (bad standing)")
        self.chk_hostile.toggled.connect(self._emit_changed)
        v.addWidget(self.chk_hostile)

        self.chk_incursions = QCheckBox("Avoid incursion systems")
        self.chk_incursions.toggled.connect(self._emit_changed)
        v.addWidget(self.chk_incursions)

        act_row = QHBoxLayout()
        b_rev = QPushButton("Reverse route")
        b_rev.clicked.connect(self.reverse)
        b_copy = QPushButton("Copy route to clipboard")
        b_copy.clicked.connect(self._copy_route)
        act_row.addWidget(b_rev)
        act_row.addWidget(b_copy)
        v.addLayout(act_row)

        # -- results --------------------------------------------------------
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Mode", "From", "To", "LY", "Fuel", "Reactivate", "Fatigue", "OK"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.table)

        self.totals = QLabel("Add 2+ waypoints to plan a route.")
        self.totals.setWordWrap(True)
        self.totals.setStyleSheet("font-weight:bold")
        v.addWidget(self.totals)

    # ---- exposed getters --------------------------------------------------
    def systems(self):
        return [wp.system for wp in self.waypoints]

    def modes(self):
        return self.route_modes

    def strategy(self) -> str:
        return "min_reactivation" if self.chk_reactivation.isChecked() else "min_time"

    def minimize(self) -> str:
        return ("only_jumps", "jumps", "fuel")[self.cmb_min.currentIndex()]

    def gate_pref(self) -> str:
        return ("fast", "safe", "insecure")[self.cmb_gate.currentIndex()]

    def avoid_incursions(self) -> bool:
        return self.chk_incursions.isChecked()

    def policy(self) -> int:
        return self.cmb_policy.currentIndex()

    def selected_system(self):
        row = self.wp_list.currentRow()
        if 0 <= row < len(self.waypoints):
            return self.waypoints[row].system
        return self.waypoints[0].system if self.waypoints else None

    # ---- mutation ---------------------------------------------------------
    def add_system(self, system_id: int):
        self._add_system(system_id)

    def _docks(self, system_id):
        """Docks in a system, with the current ship + hostile-standing filter."""
        if self.ctx.universe is None:
            return []
        return docks_for_system(
            self.ctx.universe, self.ctx.dockables, self.ctx.current_ship(), system_id,
            standings=getattr(self.ctx, "standings", None),
            hostile_threshold=self.ctx.hostile_threshold(),
            exclude_hostile=self.chk_hostile.isChecked())

    def select_system(self, system_id: int):
        """Select an existing waypoint by system (no-op if not a waypoint)."""
        for i, wp in enumerate(self.waypoints):
            if wp.system.id == system_id:
                self.wp_list.setCurrentRow(i)
                return

    def _add_system(self, system_id):
        uni = self.ctx.universe
        if not uni or system_id not in uni.systems:
            return
        sid = int(system_id)
        # Don't add a system that's already the current last waypoint (no
        # back-to-back duplicates); the same system later in the route is fine.
        if self.waypoints and self.waypoints[-1].system.id == sid:
            return
        new_sys = uni.systems[sid]
        self.ctx.ensure_public_structures(new_sys)

        # Manual add: just add the waypoint exactly as given. Out-of-range or
        # high-sec legs are flagged in the table; use Auto-route to fill gaps
        # between your waypoints without discarding them.
        self.waypoints.append(Waypoint(new_sys))
        if len(self.waypoints) > 1:
            self.route_modes.append("jump")
        self._rebuild()
        self._emit_changed()

    def reverse(self):
        self.waypoints.reverse()
        self.route_modes = list(reversed(self.route_modes))  # keep gate/jump per leg
        self._rebuild()
        self._emit_changed()

    def append_path(self, systems, modes):
        """Append a computed path (systems after the current last) + its modes."""
        for s in systems:
            self.waypoints.append(Waypoint(s))
        self.route_modes.extend(modes)
        self._rebuild()
        self._emit_changed()

    def set_busy(self, busy: bool):
        self.busy.setVisible(busy)
        self.b_auto.setEnabled(not busy)

    def _search_menu(self, pos):
        item = self.search_results.itemAt(pos)
        if not item:
            return
        sid = item.data(_ROLE_SYS)
        menu = QMenu(self)
        act_add = menu.addAction("Add as waypoint")
        act_info = menu.addAction("Show system info")
        chosen = menu.exec(self.search_results.mapToGlobal(pos))
        if chosen == act_add:
            self._add_system(sid)
        elif chosen == act_info:
            self.show_system_info(sid)

    def set_route(self, systems, modes):
        self.waypoints = [Waypoint(s) for s in systems]
        self.route_modes = list(modes)
        self._rebuild()
        self._emit_changed()

    def remove_system(self, system_id: int):
        for i, wp in enumerate(self.waypoints):
            if wp.system.id == system_id:
                del self.waypoints[i]
                self.route_modes = ["jump"] * max(0, len(self.waypoints) - 1)
                self._rebuild()
                self._emit_changed()
                return

    def _remove_selected(self):
        row = self.wp_list.currentRow()
        if 0 <= row < len(self.waypoints):
            del self.waypoints[row]
            self.route_modes = ["jump"] * max(0, len(self.waypoints) - 1)
            self._rebuild()
            self._emit_changed()

    def _up(self):
        r = self.wp_list.currentRow()
        if r > 0:
            self.waypoints[r - 1], self.waypoints[r] = self.waypoints[r], self.waypoints[r - 1]
            self.route_modes = ["jump"] * max(0, len(self.waypoints) - 1)
            self._rebuild()
            self.wp_list.setCurrentRow(r - 1)
            self._emit_changed()

    def _down(self):
        r = self.wp_list.currentRow()
        if 0 <= r < len(self.waypoints) - 1:
            self.waypoints[r + 1], self.waypoints[r] = self.waypoints[r], self.waypoints[r + 1]
            self.route_modes = ["jump"] * max(0, len(self.waypoints) - 1)
            self._rebuild()
            self.wp_list.setCurrentRow(r + 1)
            self._emit_changed()

    def _clear(self):
        self.waypoints.clear()
        self.route_modes.clear()
        self._rebuild()
        self._emit_changed()

    def _wp_menu(self, pos):
        item = self.wp_list.itemAt(pos)
        if not item:
            return
        # Read item data BEFORE exec(): the menu runs a nested event loop and a
        # background refresh can delete the underlying C++ item meanwhile.
        sid = item.data(_ROLE_SYS)
        uid = item.data(_ROLE_UID)
        menu = QMenu(self)
        act_sysinfo = menu.addAction("Show system info")
        act_info = menu.addAction("Show station info")
        act_wp = menu.addAction("Set in-game destination")
        menu.addSeparator()
        act_remove = menu.addAction("Remove waypoint")
        act_clear = menu.addAction("Clear all waypoints")
        chosen = menu.exec(self.wp_list.mapToGlobal(pos))
        if chosen == act_remove:
            self._remove_by_uid(uid)
        elif chosen == act_clear:
            self._clear()
        elif chosen == act_info:
            self.show_station_info(sid)
        elif chosen == act_sysinfo:
            self.show_system_info(sid)
        elif chosen == act_wp:
            self.ctx.set_ingame_waypoint(sid)

    def _remove_by_uid(self, uid):
        wp = self._uid_map.get(uid)
        if wp is None:
            return
        self.waypoints = [w for w in self.waypoints if w is not wp]
        self.route_modes = ["jump"] * max(0, len(self.waypoints) - 1)
        self._rebuild()
        self._emit_changed()

    def _on_rows_moved(self, *args):
        # Rebuild waypoint order from the list widget's current item order.
        new_order = []
        for i in range(self.wp_list.count()):
            uid = self.wp_list.item(i).data(_ROLE_UID)
            wp = self._uid_map.get(uid)
            if wp is not None:
                new_order.append(wp)
        if len(new_order) == len(self.waypoints):
            self.waypoints = new_order
            self.route_modes = ["jump"] * max(0, len(self.waypoints) - 1)
            self._rebuild()
            self._emit_changed()

    # ---- rendering --------------------------------------------------------
    def _rebuild(self):
        keep = self.wp_list.currentRow()
        self.wp_list.blockSignals(True)
        self.wp_list.clear()
        self._uid_map.clear()
        ship = self.ctx.current_ship()
        uni = self.ctx.universe
        for i, wp in enumerate(self.waypoints):
            uid = next(self._uid)
            self._uid_map[uid] = wp
            eff = self._effective(wp)
            suffix = f"  —  {eff.name}" if eff else self._empty_suffix(wp)
            it = QListWidgetItem(f"{i}: {wp.system.name}  ({wp.system.security:.1f}){suffix}")
            it.setData(_ROLE_SYS, wp.system.id)
            it.setData(_ROLE_UID, uid)
            if eff:
                it.setToolTip(f"{eff.name} [{eff.kind}]\n{eff.note}\n"
                              "(right-click → Show station info for image)")
            self.wp_list.addItem(it)
        if 0 <= keep < len(self.waypoints):
            self.wp_list.setCurrentRow(keep)
        self.wp_list.blockSignals(False)
        self._update_pick()

    def _effective(self, wp: Waypoint):
        if self.ctx.universe is None:
            return None
        return effective_dock(wp, self._docks(wp.system.id))

    def _empty_suffix(self, wp: Waypoint) -> str:
        uni = self.ctx.universe
        if uni is None:
            return ""
        if self._docks(wp.system.id):
            return "  —  (no usable docking)"
        if uni.stations:  # stations loaded and there is genuinely nothing here
            return "  —  (no station/structure)"
        return ""

    def show_station_info(self, system_id):
        from ..dialogs import StationInfoDialog
        from ..models import best_dock
        uni = self.ctx.universe
        if uni is None or system_id not in uni.systems:
            return
        opts = self._docks(system_id)
        wp = next((w for w in self.waypoints if w.system.id == system_id), None)
        eff = effective_dock(wp, opts) if wp else best_dock(opts)
        sysname = uni.systems[system_id].name
        if not eff:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Station info",
                                    f"{sysname}: no known dockable station or structure.")
            return
        standing = self.ctx.standing_of(eff.owner_id) if eff.owner_id else None
        dlg = StationInfoDialog(self, sysname, eff, standing)
        self.ctx.request_station_image(eff.type_id, dlg.set_image)
        if eff.owner_id:
            self.ctx.request_entity_name(eff.owner_id, dlg.set_owner_name)
        dlg.exec()

    def show_system_info(self, system_id):
        from PySide6.QtWidgets import QMessageBox
        uni = self.ctx.universe
        if uni is None or system_id not in uni.systems:
            return
        s = uni.systems[system_id]
        region = uni.region_names.get(s.region_id, str(s.region_id))
        kind = ("high-sec" if s.security >= 0.5 else
                "low-sec" if s.security > 0.0 else "null-sec")
        QMessageBox.information(
            self, f"System: {s.name}",
            f"<b>{s.name}</b>  ({s.security:.2f}, {kind})<br>"
            f"Region: {region}<br>"
            f"Jump target: {'yes' if s.jumpable else 'no (high-sec)'}<br>"
            f'<a href="https://evemaps.dotlan.net/system/{s.name.replace(" ", "_")}">'
            "Open in Dotlan</a>")

    def refresh(self):
        self._rebuild()

    def _on_select(self):
        self._update_pick()
        self.changed.emit()

    def _update_pick(self):
        row = self.wp_list.currentRow()
        self.cmb_pick.blockSignals(True)
        self.cmb_pick.clear()
        if not (0 <= row < len(self.waypoints)) or self.ctx.universe is None:
            self.lbl_dock.setText("Dock:")
            self.cmb_pick.setEnabled(False)
            self.cmb_pick.blockSignals(False)
            return
        wp = self.waypoints[row]
        self.ctx.ensure_public_structures(wp.system)
        opts = self._docks(wp.system.id)
        self.lbl_dock.setText(f"Dock at {wp.system.name}:")
        self.cmb_pick.setEnabled(True)
        self.cmb_pick.addItem("Auto (best available)", None)
        for o in opts:
            self.cmb_pick.addItem(f"{_STATUS_ICON[o.can_dock]} {o.name}  [{o.kind}]", o.key())
        # reflect current choice
        if wp.chosen is not None:
            idx = self.cmb_pick.findData(wp.chosen.key())
            if idx >= 0:
                self.cmb_pick.setCurrentIndex(idx)
        self.cmb_pick.blockSignals(False)

    def _on_pick(self, idx):
        row = self.wp_list.currentRow()
        if not (0 <= row < len(self.waypoints)):
            return
        key = self.cmb_pick.currentData()
        wp = self.waypoints[row]
        if key is None:
            wp.chosen = None
        else:
            opts = docks_for_system(self.ctx.universe, self.ctx.dockables,
                                    self.ctx.current_ship(), wp.system.id)
            wp.chosen = next((o for o in opts if o.key() == key), None)
        self._rebuild()
        self.wp_list.setCurrentRow(row)
        self._emit_changed()

    # ---- results ----------------------------------------------------------
    def display_plan(self, plan):
        self.table.setRowCount(len(plan.legs))
        for i, leg in enumerate(plan.legs):
            if leg.mode == "gate":
                vals = ["gate", leg.src.name, leg.dst.name, f"{leg.distance_ly:.1f}",
                        "—", "—", f"{leg.fatigue_after_min:.0f}m", "✓"]
            else:
                ok = "✓" if leg.in_range else f"✗ {leg.reason}"
                vals = ["jump", leg.src.name, leg.dst.name, f"{leg.distance_ly:.2f}",
                        f"{leg.fuel:,}", f"{leg.cooldown_min:.1f}m",
                        f"{leg.fatigue_after_min:.0f}m", ok]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if leg.mode == "jump" and not leg.in_range:
                    it.setForeground(Qt.GlobalColor.red)
                self.table.setItem(i, c, it)
        if plan.legs:
            hrs = plan.total_time_min / 60.0
            warn = ("" if plan.all_in_range else
                    "   ⚠ some legs invalid (range / hi-sec) — use Auto-route to bridge")
            self.totals.setText(
                f"{plan.jumps} jump(s), {plan.gates} gate(s) · "
                f"{plan.total_fuel:,} isotopes · time ≈ {plan.total_time_min:.0f} min "
                f"({hrs:.1f} h) · peak fatigue {plan.peak_fatigue_min:.0f}m · "
                f"peak reactivation {plan.peak_reactivation_min:.1f}m{warn}")
        else:
            self.totals.setText("Add 2+ waypoints to plan a route.")

    # ---- search -----------------------------------------------------------
    def _do_search(self):
        uni = self.ctx.universe
        if not uni:
            return
        self.search_results.clear()
        for s in uni.search(self.search.text()):
            it = QListWidgetItem(f"{s.name}  ({s.security:.1f})")
            it.setData(_ROLE_SYS, s.id)
            self.search_results.addItem(it)

    # ---- copy -------------------------------------------------------------
    def _copy_route(self):
        lines = []
        for i, wp in enumerate(self.waypoints):
            mode = ""
            if i > 0 and i - 1 < len(self.route_modes):
                mode = f"[{self.route_modes[i - 1]}] "
            eff = effective_dock(wp, self._docks(wp.system.id))
            dock = f" - {eff.name}" if eff else ""
            lines.append(f"{mode}{wp.system.name}{dock}")
        QGuiApplication.clipboard().setText("\n".join(lines))
        self.totals.setText(f"Copied {len(lines)} waypoint(s) to clipboard.")

    # ---- persistence ------------------------------------------------------
    def _emit_changed(self):
        self.changed.emit()

    def state(self) -> dict:
        return {
            "policy": self.cmb_policy.currentIndex(),
            "minimize": self.cmb_min.currentIndex(),
            "gate": self.cmb_gate.currentIndex(),
            "min_reactivation": self.chk_reactivation.isChecked(),
            "exclude_hostile": self.chk_hostile.isChecked(),
            "avoid_incursions": self.chk_incursions.isChecked(),
        }

    def restore(self, s: dict):
        widgets = (self.cmb_policy, self.cmb_min, self.cmb_gate,
                   self.chk_reactivation, self.chk_hostile, self.chk_incursions)
        for w in widgets:
            w.blockSignals(True)
        self.cmb_policy.setCurrentIndex(int(s.get("policy", 0)))
        self.cmb_min.setCurrentIndex(int(s.get("minimize", 1)))
        self.cmb_gate.setCurrentIndex(int(s.get("gate", 0)))
        self.chk_reactivation.setChecked(bool(s.get("min_reactivation", False)))
        self.chk_hostile.setChecked(bool(s.get("exclude_hostile", False)))
        self.chk_incursions.setChecked(bool(s.get("avoid_incursions", False)))
        for w in widgets:
            w.blockSignals(False)
