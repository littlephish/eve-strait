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
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
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
    gate_assist_requested = Signal()

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

        self.chk_nodocks = QCheckBox("Just passing through (don't pick docks)")
        self.chk_nodocks.setToolTip(
            "For subcaps and freighters warping gate to gate. Waypoints stop "
            "naming a station, and the dock picker is disabled. The docking "
            "filter above still applies to routing.")
        self.chk_nodocks.toggled.connect(self._on_nodocks_toggled)
        v.addWidget(self.chk_nodocks)

        self.chk_gates = QCheckBox("Allow gates to reduce the number of jumps")
        self.chk_gates.setChecked(True)
        self.chk_gates.setToolTip(
            "Use stargates wherever they save jumps - regional gates that span "
            "further than you can jump, and gating out of hi-sec to a jumpable "
            "system. Unchecked = jump drive only.")
        self.chk_gates.toggled.connect(self._on_gates_toggled)
        v.addWidget(self.chk_gates)

        opt2 = QHBoxLayout()
        opt2.addWidget(QLabel("     Balance:"))
        self.cmb_balance = QComboBox()
        # Label -> cost of one jump measured in gate hops. Below 1 means a
        # jump is cheaper than a single gate hop, i.e. "gate only when it
        # genuinely buys something" -- which is the usual capital preference.
        for label, cost in (
            ("Jump whenever possible", 0.3),
            ("Prefer jumps - gate only when it saves several", 0.6),
            ("Balanced", 1.5),
            ("Prefer gates - save fuel & fatigue", 6.0),
            ("Gate whenever possible", 30.0),
        ):
            self.cmb_balance.addItem(label, cost)
        self.cmb_balance.setCurrentIndex(1)
        self.cmb_balance.setToolTip(
            "How eagerly gates are used instead of jumps.\n"
            "Jump-heavy settings are fast and keep you off gates; gate-heavy\n"
            "settings save fuel and fatigue but mean long gate chains.\n"
            "A regional gate that spans further than you can jump is taken at\n"
            "any setting, because no number of jumps replaces it.")
        self.cmb_balance.currentIndexChanged.connect(self._emit_changed)
        opt2.addWidget(self.cmb_balance, 1)
        v.addLayout(opt2)

        self.chk_ansiblex = QCheckBox("Use Ansiblex network")
        self.chk_ansiblex.setChecked(True)
        self.chk_ansiblex.setToolTip(
            "Route through your configured Ansiblex jump gates "
            "(File → Ansiblex jump gates…).")
        self.chk_ansiblex.toggled.connect(self._emit_changed)
        v.addWidget(self.chk_ansiblex)

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

        self.chk_kills = QCheckBox("Steer around recent kills")
        self.chk_kills.setToolTip(
            "Bias the route away from systems with player kills in the last "
            "hour. A preference, not a hard block: a route is never made "
            "impossible by it.")
        self.chk_kills.toggled.connect(self._emit_changed)
        v.addWidget(self.chk_kills)

        act_row = QHBoxLayout()
        b_assist = QPushButton("Gate assist…")
        b_assist.setToolTip("Compare a pure jump route against jump+gate, and show "
                            "where a short gate run replaces several jumps.")
        b_assist.clicked.connect(self.gate_assist_requested)
        act_row.addWidget(b_assist)
        b_rev = QPushButton("Reverse route")
        b_rev.clicked.connect(self.reverse)
        b_copy = QPushButton("Copy route to clipboard")
        b_copy.clicked.connect(self._copy_route)
        act_row.addWidget(b_rev)
        act_row.addWidget(b_copy)
        v.addLayout(act_row)

        self.b_saved = QPushButton("Saved routes")
        self.b_saved.setToolTip(
            "Store the current waypoints under a name and load them back later.")
        self.b_saved.clicked.connect(self._saved_menu)
        v.addWidget(self.b_saved)

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

    def _on_gates_toggled(self, on: bool):
        self.cmb_balance.setEnabled(on)
        self._emit_changed()

    def minimize(self) -> str:
        return "jumps" if self.chk_gates.isChecked() else "only_jumps"

    def jump_cost(self) -> float:
        """Cost of one jump measured in gate hops (<1 = jumps are cheaper)."""
        return float(self.cmb_balance.currentData() or 0.6)

    def use_ansiblex(self) -> bool:
        return self.chk_ansiblex.isChecked()

    def gate_pref(self) -> str:
        return ("fast", "safe", "insecure")[self.cmb_gate.currentIndex()]

    def avoid_incursions(self) -> bool:
        return self.chk_incursions.isChecked()

    def avoid_kills(self) -> bool:
        return self.chk_kills.isChecked()

    def pick_docks(self) -> bool:
        """False when the user is just passing through and wants no docks."""
        return not self.chk_nodocks.isChecked()

    def _on_nodocks_toggled(self, _on):
        self._rebuild()
        self._emit_changed()

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
            exclude_hostile=self.chk_hostile.isChecked(),
            relation=self.ctx.owner_relation_cached,
            has_rights=self.ctx.has_docking_rights,
            starbases=self.ctx.starbases_in(system_id))

    def set_origin(self, system_id: int):
        """Make a system the first waypoint, keeping the rest of the route."""
        uni = self.ctx.universe
        if not uni or system_id not in uni.systems:
            return
        if self.waypoints and self.waypoints[0].system.id == system_id:
            return
        # Drop a stale origin only if it was never travelled from.
        self.waypoints.insert(0, Waypoint(uni.systems[int(system_id)]))
        self.route_modes = ["jump"] * max(0, len(self.waypoints) - 1)
        self._rebuild()
        self.wp_list.setCurrentRow(0)
        self._emit_changed()

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
        act_wp, wp_actions = self.ctx.add_waypoint_menu(menu)
        act_avoid = menu.addAction(
            "Stop avoiding this system" if self.ctx.is_avoided(sid)
            else "Avoid this system")
        menu.addSeparator()
        act_pin = menu.addAction("Save current dock as default for this system")
        act_unpin = None
        if self._pinned(sid):
            act_unpin = menu.addAction("Clear saved default dock")
        menu.addSeparator()
        act_remove = menu.addAction("Remove waypoint")
        act_clear = menu.addAction("Clear all waypoints")
        chosen = menu.exec(self.wp_list.mapToGlobal(pos))
        if chosen == act_pin:
            self._pin_dock(uid, sid)
        elif act_unpin is not None and chosen == act_unpin:
            self._pin_dock(uid, sid, clear=True)
        elif chosen == act_remove:
            self._remove_by_uid(uid)
        elif chosen == act_clear:
            self._clear()
        elif chosen == act_info:
            self.show_station_info(sid)
        elif chosen == act_sysinfo:
            self.show_system_info(sid)
        elif chosen == act_wp:
            self.ctx.set_ingame_waypoint(sid)
        elif chosen in wp_actions:
            self.ctx.set_ingame_waypoint(sid, wp_actions[chosen])
        elif chosen == act_avoid:
            self.ctx.toggle_avoid(sid)

    def _pin_dock(self, uid, system_id, clear: bool = False):
        """Remember (or forget) the default dock for this system."""
        from ... import config
        from PySide6.QtWidgets import QMessageBox
        if clear:
            config.set_default_dock(system_id, None)
            self._rebuild()
            return
        wp = self._uid_map.get(uid)
        eff = self._effective(wp) if wp else None
        if not eff:
            QMessageBox.information(self, "Default dock",
                                    "No dock selected for this system yet.")
            return
        config.set_default_dock(system_id, eff.name)
        self._rebuild()
        self.totals.setText(f"Default dock for this system saved: {eff.name}")

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
            eff = self._effective(wp) if self.pick_docks() else None
            if not self.pick_docks():
                suffix = ""
            else:
                suffix = f"  -  {eff.name}" if eff else self._empty_suffix(wp)
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

    def _pinned(self, system_id) -> str | None:
        from ... import config
        return config.get_default_docks().get(str(system_id))

    def _effective(self, wp: Waypoint):
        if self.ctx.universe is None:
            return None
        return effective_dock(wp, self._docks(wp.system.id), self._pinned(wp.system.id))

    def _empty_suffix(self, wp: Waypoint) -> str:
        uni = self.ctx.universe
        if uni is None:
            return ""
        opts = self._docks(wp.system.id)
        tether = next((o for o in opts if o.can_tether), None)
        if tether is not None:
            # No dock, but a capital can still tether / sit in a POS shield.
            return f"  -  tether: {tether.name}"
        if opts:
            return "  -  (no usable docking)"
        if uni.stations:  # stations loaded and there is genuinely nothing here
            return "  -  (no station/structure)"
        return ""

    def show_station_info(self, system_id):
        from ..dialogs import StationInfoDialog
        from ..models import best_dock
        uni = self.ctx.universe
        if uni is None or system_id not in uni.systems:
            return
        opts = self._docks(system_id)
        wp = next((w for w in self.waypoints if w.system.id == system_id), None)
        pinned = self._pinned(system_id)
        eff = (effective_dock(wp, opts, pinned) if wp
               else best_dock(opts, system_id, pinned))
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
            self.ctx.request_owner_details(eff.owner_id, dlg.set_owner_details)
        dlg.exec()

    # ---- saved routes -----------------------------------------------------
    def _saved_menu(self):
        """Save / load / delete named routes.

        Waypoints are stored, not the planned path: the plan is recomputed on
        load so a saved route picks up current sov, kills and Ansiblex links
        rather than replaying a stale one.
        """
        from ... import config
        routes = config.get_saved_routes()
        menu = QMenu(self)
        act_save = menu.addAction("Save current route as...")
        act_save.setEnabled(len(self.waypoints) >= 2)
        if not routes:
            menu.addSeparator()
            none = menu.addAction("(no saved routes)")
            none.setEnabled(False)
        load_acts, del_acts = {}, {}
        if routes:
            menu.addSeparator()
            for name in sorted(routes):
                stops = routes[name].get("systems", [])
                a = menu.addAction(f"{name}  ({len(stops)} stops)")
                a.setToolTip(" → ".join(stops))
                load_acts[a] = name
            sub = menu.addMenu("Delete")
            for name in sorted(routes):
                del_acts[sub.addAction(name)] = name
        chosen = menu.exec(self.b_saved.mapToGlobal(
            self.b_saved.rect().bottomLeft()))
        if chosen is None:
            return
        if chosen == act_save:
            self._save_route()
        elif chosen in load_acts:
            self._load_route(load_acts[chosen], routes[load_acts[chosen]])
        elif chosen in del_acts:
            name = del_acts[chosen]
            if QMessageBox.question(self, "Delete route",
                                    f"Delete the saved route '{name}'?") \
                    == QMessageBox.StandardButton.Yes:
                config.delete_route(name)

    def _save_route(self):
        from ... import config
        names = [wp.system.name for wp in self.waypoints]
        default = f"{names[0]} to {names[-1]}"
        name, ok = QInputDialog.getText(self, "Save route", "Name:", text=default)
        name = (name or "").strip()
        if not ok or not name:
            return
        if name in config.get_saved_routes() and QMessageBox.question(
                self, "Overwrite",
                f"'{name}' already exists. Replace it?") \
                != QMessageBox.StandardButton.Yes:
            return
        ship = None
        try:
            ship = self.ctx.ship.state().get("ship")
        except AttributeError:
            pass
        config.save_route(name, names, ship)
        self.ctx.statusBar().showMessage(
            f"Saved '{name}': {len(names)} waypoints.", 6000)

    def _load_route(self, name: str, data: dict):
        uni = self.ctx.universe
        if uni is None:
            return
        wanted = data.get("systems") or []
        found, missing = [], []
        for n in wanted:
            s = uni.by_name(n)
            (found if s else missing).append(s or n)
        if not found:
            QMessageBox.warning(self, "Load route",
                                f"None of the systems in '{name}' exist in the "
                                "current map data.")
            return

        self._clear()
        for s in found:
            self._add_system(s.id)

        # The ship is stored too, since a jump-freighter route and a dread
        # route through the same systems are not the same route.
        note = ""
        ship = data.get("ship")
        if ship:
            try:
                cur = self.ctx.ship.state()
                if cur.get("ship") != ship:
                    self.ctx.ship.restore(dict(cur, ship=ship))
                    note = f" Ship set to the one saved with it."
            except AttributeError:
                pass
        if missing:
            note += f" Skipped unknown: {', '.join(missing)}."
        self.ctx.statusBar().showMessage(
            f"Loaded '{name}': {len(found)} waypoints.{note}", 8000)
        self._emit_changed()

    def show_system_info(self, system_id):
        from ..dialogs import SystemInfoDialog
        uni = self.ctx.universe
        if uni is None or system_id not in uni.systems:
            return
        s = uni.systems[system_id]
        SystemInfoDialog(
            self, s,
            f"Region: {uni.region_names.get(s.region_id, str(s.region_id))}",
            self.ctx.sov_of(s.id),
            self.ctx.system_intel(s.id),
            cyno_cb=self.ctx.check_cyno_activity,
        ).exec()

    def refresh(self):
        self._rebuild()

    def _on_select(self):
        self._update_pick()
        self.changed.emit()

    def _update_pick(self):
        row = self.wp_list.currentRow()
        self.cmb_pick.blockSignals(True)
        self.cmb_pick.clear()
        if not self.pick_docks():
            self.lbl_dock.setText("Dock: (passing through)")
            self.cmb_pick.setEnabled(False)
            self.cmb_pick.blockSignals(False)
            return
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
            if leg.mode == "bridge":
                vals = ["ansiblex", leg.src.name, leg.dst.name,
                        f"{leg.distance_ly:.2f}", "-",
                        f"{leg.cooldown_min:.1f}m",
                        f"{leg.fatigue_after_min:.0f}m", "✓"]
            elif leg.mode == "gate":
                vals = ["gate", leg.src.name, leg.dst.name, f"{leg.distance_ly:.1f}",
                        "-", "-", f"{leg.fatigue_after_min:.0f}m", "✓"]
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
                    "   ⚠ some legs invalid (range / hi-sec) - use Auto-route to bridge")
            bridges = f", {plan.bridges} ansiblex" if plan.bridges else ""
            self.totals.setText(
                f"{plan.jumps} jump(s), {plan.gates} gate(s){bridges} · "
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
            eff = self._effective(wp)
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
            "allow_gates": self.chk_gates.isChecked(),
            "balance": self.cmb_balance.currentIndex(),
            "use_ansiblex": self.chk_ansiblex.isChecked(),
            "gate": self.cmb_gate.currentIndex(),
            "min_reactivation": self.chk_reactivation.isChecked(),
            "exclude_hostile": self.chk_hostile.isChecked(),
            "avoid_incursions": self.chk_incursions.isChecked(),
            "avoid_kills": self.chk_kills.isChecked(),
            "no_docks": self.chk_nodocks.isChecked(),
        }

    def restore(self, s: dict):
        widgets = (self.cmb_policy, self.chk_gates, self.cmb_balance, self.chk_ansiblex,
                   self.cmb_gate, self.chk_reactivation, self.chk_hostile,
                   self.chk_incursions)
        for w in widgets:
            w.blockSignals(True)
        self.cmb_policy.setCurrentIndex(int(s.get("policy", 0)))
        # "minimize" is the pre-0.2 three-way combo (0 = only jumps).
        allow = s.get("allow_gates", s.get("minimize", 1) != 0)
        self.chk_gates.setChecked(bool(allow))
        # Default to "Prefer jumps" (index 1); pre-0.3 configs stored a
        # gate-hop count instead of a preset index.
        self.cmb_balance.setCurrentIndex(int(s.get("balance", 1)))
        self.cmb_balance.setEnabled(bool(allow))
        self.chk_ansiblex.setChecked(bool(s.get("use_ansiblex", True)))
        self.cmb_gate.setCurrentIndex(int(s.get("gate", 0)))
        self.chk_reactivation.setChecked(bool(s.get("min_reactivation", False)))
        self.chk_hostile.setChecked(bool(s.get("exclude_hostile", False)))
        self.chk_incursions.setChecked(bool(s.get("avoid_incursions", False)))
        self.chk_kills.setChecked(bool(s.get("avoid_kills", False)))
        self.chk_nodocks.setChecked(bool(s.get("no_docks", False)))
        for w in widgets:
            w.blockSignals(False)
