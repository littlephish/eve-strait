"""Main application window."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..data import docking
from ..data.ships import SHIPS, SHIPS_BY_NAME, Ship, Skills
from ..data.universe import System, Universe
from ..esi import auth
from ..esi.client import EsiClient
from ..jump import mechanics, router
from .map_view import MapView
from .workers import Worker

_ROLE_SYS = Qt.ItemDataRole.UserRole


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EVE Jump Planner")
        self.resize(1400, 900)

        self.universe: Universe | None = None
        self.map_view: MapView | None = None
        self.waypoints: list[System] = []
        self.route_modes: list[str] = []   # per-leg "jump"/"gate"
        self.token: auth.Token | None = auth.load_saved()
        self.esi: EsiClient | None = None
        self._workers: list[Worker] = []
        self._dockables: list = []
        self._built = False

        self._status = QLabel("Loading New Eden map data...")
        self.setCentralWidget(self._status)

        self._build_menu()
        self._build_ship_dock()
        self._build_route_dock()
        self._build_character_dock()
        self._refresh_login_label()
        self._load_settings()
        self._built = True

        self._load_universe()

    # ------------------------------------------------------------------ menu
    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        act_cid = QAction("Set EVE Client ID...", self)
        act_cid.triggered.connect(self._set_client_id)
        act_scopes = QAction("Set ESI scopes...", self)
        act_scopes.triggered.connect(self._set_scopes)
        act_reload = QAction("Reload map data", self)
        act_reload.triggered.connect(self._reload_map)
        act_logout = QAction("Log out", self)
        act_logout.triggered.connect(self._logout)
        act_quit = QAction("Quit", self)
        act_quit.triggered.connect(self.close)
        for a in (act_cid, act_scopes, act_reload, act_logout, act_quit):
            m.addAction(a)

    # ---------------------------------------------------------- ship & skills
    def _build_ship_dock(self):
        dock = QDockWidget("Ship & Skills", self)
        w = QWidget()
        form = QFormLayout(w)

        self.ship_combo = QComboBox()
        for s in SHIPS:
            self.ship_combo.addItem(f"{s.name}  ·  {s.hull_class}", s.name)
        self.ship_combo.currentIndexChanged.connect(self._settings_changed)
        form.addRow("Ship", self.ship_combo)

        self.sp_jdc = self._skill_spin(4)
        self.sp_jdo = self._skill_spin(5)
        self.sp_jfc = self._skill_spin(4)
        self.sp_jf = self._skill_spin(4)
        form.addRow("Jump Drive Calibration", self.sp_jdc)
        form.addRow("Jump Drive Operation", self.sp_jdo)
        form.addRow("Jump Fuel Conservation", self.sp_jfc)
        form.addRow("Jump Freighters (JF only)", self.sp_jf)

        self.sp_fatigue = QDoubleSpinBox()
        self.sp_fatigue.setRange(0, 100)
        self.sp_fatigue.setSuffix(" %")
        self.sp_fatigue.valueChanged.connect(self._settings_changed)
        form.addRow("Fatigue reduction (implants)", self.sp_fatigue)

        self.chk_bridge = self._check("Plan reach as bridge/portal")
        self.chk_bridge.toggled.connect(self._settings_changed)
        form.addRow(self.chk_bridge)

        self.lbl_range = QLabel("-")
        self.lbl_fuel = QLabel("-")
        self.lbl_iso = QLabel("-")
        self.lbl_bridge = QLabel("-")
        for lbl in (self.lbl_range, self.lbl_fuel, self.lbl_iso, self.lbl_bridge):
            lbl.setStyleSheet("font-weight:bold;color:#5bc0eb")
        form.addRow("Max jump range", self.lbl_range)
        form.addRow("Fuel / light year", self.lbl_fuel)
        form.addRow("Isotope", self.lbl_iso)
        form.addRow("Bridge range", self.lbl_bridge)

        dock.setWidget(w)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def _skill_spin(self, default: int) -> QSpinBox:
        sp = QSpinBox()
        sp.setRange(0, 5)
        sp.setValue(default)
        sp.valueChanged.connect(self._settings_changed)
        return sp

    @staticmethod
    def _check(text: str):
        from PySide6.QtWidgets import QCheckBox
        return QCheckBox(text)

    # ------------------------------------------------------------- route dock
    def _build_route_dock(self):
        dock = QDockWidget("Route", self)
        w = QWidget()
        v = QVBoxLayout(w)

        # search
        sbox = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find system by name...")
        self.search.returnPressed.connect(self._do_search)
        btn_find = QPushButton("Find")
        btn_find.clicked.connect(self._do_search)
        sbox.addWidget(self.search)
        sbox.addWidget(btn_find)
        v.addLayout(sbox)

        self.search_results = QListWidget()
        self.search_results.setMaximumHeight(90)
        self.search_results.itemDoubleClicked.connect(self._add_from_search)
        v.addWidget(self.search_results)

        hint = QLabel("Double-click a search result or click the map to add a waypoint. "
                      "First waypoint = origin.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888")
        v.addWidget(hint)

        # waypoints
        v.addWidget(QLabel("Waypoints"))
        self.wp_list = QListWidget()
        self.wp_list.itemSelectionChanged.connect(self._recalc)
        v.addWidget(self.wp_list)

        row = QHBoxLayout()
        for text, slot in (
            ("↑", self._wp_up), ("↓", self._wp_down),
            ("Remove", self._wp_remove), ("Clear", self._wp_clear),
        ):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        v.addLayout(row)

        dock_row = QHBoxLayout()
        dock_row.addWidget(QLabel("Docking:"))
        self.cmb_dock = QComboBox()
        self.cmb_dock.addItems([
            "No docking filter", "Require any docking", "Prefer safe docking only"])
        self.cmb_dock.currentIndexChanged.connect(self._render_structs)
        dock_row.addWidget(self.cmb_dock)
        v.addLayout(dock_row)

        row2 = QHBoxLayout()
        self.cmb_min = QComboBox()
        self.cmb_min.addItems(["fewest jumps", "least fuel"])
        b_auto = QPushButton("Auto-route origin → last")
        b_auto.clicked.connect(self._auto_route)
        row2.addWidget(self.cmb_min)
        row2.addWidget(b_auto)
        v.addLayout(row2)

        # results
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["From", "To", "LY", "Fuel", "Cooldown", "Fatigue", "OK"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.table)

        self.totals = QLabel("-")
        self.totals.setWordWrap(True)
        self.totals.setStyleSheet("font-weight:bold")
        v.addWidget(self.totals)

        dock.setWidget(w)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    # --------------------------------------------------------- character dock
    def _build_character_dock(self):
        dock = QDockWidget("Character & Structures", self)
        w = QWidget()
        v = QVBoxLayout(w)

        self.lbl_login = QLabel("-")
        v.addWidget(self.lbl_login)

        self.btn_login = QPushButton("Log in with EVE")
        self.btn_login.clicked.connect(self._login)
        v.addWidget(self.btn_login)

        self.btn_structs = QPushButton("Load my dockable structures")
        self.btn_structs.clicked.connect(self._load_structures)
        v.addWidget(self.btn_structs)

        v.addWidget(QLabel("Docked/asset locations (double-click to add):"))
        self.struct_list = QListWidget()
        self.struct_list.itemDoubleClicked.connect(self._add_from_struct)
        v.addWidget(self.struct_list)

        dock.setWidget(w)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

    # ---------------------------------------------------------------- loading
    def _load_universe(self):
        worker = Worker(Universe.load)
        worker.progress.connect(self._status.setText)
        worker.finished_ok.connect(self._on_universe)
        worker.failed.connect(lambda msg: self._status.setText(
            f"Failed to load map data:\n{msg}\n\nCheck your connection and use "
            "File → Reload map data."))
        self._run(worker)

    def _on_universe(self, universe: Universe):
        self.universe = universe
        self.map_view = MapView(universe)
        self.map_view.system_clicked.connect(self._on_map_click)
        self.setCentralWidget(self.map_view)
        self._recalc()

    def _reload_map(self):
        config.SDE_CSV_PATH.unlink(missing_ok=True)
        self._status = QLabel("Reloading map data...")
        self.setCentralWidget(self._status)
        self._load_universe()

    # --------------------------------------------------------------- helpers
    def _run(self, worker: Worker):
        self._workers.append(worker)
        worker.finished.connect(lambda: self._workers.remove(worker)
                                if worker in self._workers else None)
        worker.start()

    def current_ship(self) -> Ship:
        return SHIPS_BY_NAME[self.ship_combo.currentData()]

    def current_skills(self) -> Skills:
        return Skills(
            jump_drive_calibration=self.sp_jdc.value(),
            jump_drive_operation=self.sp_jdo.value(),
            jump_fuel_conservation=self.sp_jfc.value(),
            jump_freighters=self.sp_jf.value(),
            fatigue_reduction_pct=self.sp_fatigue.value(),
        ).clamp()

    def reach_range(self, ship: Ship, skills: Skills) -> float:
        if self.chk_bridge.isChecked() and ship.bridge_range_ly:
            return ship.bridge_range_ly
        return mechanics.max_range_ly(ship, skills)

    def _origin(self) -> System | None:
        return self.waypoints[0] if self.waypoints else None

    def _reach_system(self) -> System | None:
        """System used for the range circle: current list selection, else origin."""
        row = self.wp_list.currentRow()
        if 0 <= row < len(self.waypoints):
            return self.waypoints[row]
        return self._origin()

    # ------------------------------------------------------------- waypoints
    def _add_waypoint(self, sys: System):
        self.waypoints.append(sys)
        item = QListWidgetItem(f"{len(self.waypoints) - 1}: {sys.name}  ({sys.security:.1f})")
        item.setData(_ROLE_SYS, sys.id)
        self.wp_list.addItem(item)
        self._recalc()

    def _rebuild_wp_list(self):
        self.wp_list.blockSignals(True)
        self.wp_list.clear()
        for i, s in enumerate(self.waypoints):
            it = QListWidgetItem(f"{i}: {s.name}  ({s.security:.1f})")
            it.setData(_ROLE_SYS, s.id)
            self.wp_list.addItem(it)
        self.wp_list.blockSignals(False)

    def _wp_remove(self):
        row = self.wp_list.currentRow()
        if 0 <= row < len(self.waypoints):
            del self.waypoints[row]
            self._rebuild_wp_list()
            self._recalc()

    def _wp_up(self):
        row = self.wp_list.currentRow()
        if row > 0:
            self.waypoints[row - 1], self.waypoints[row] = self.waypoints[row], self.waypoints[row - 1]
            self._rebuild_wp_list()
            self.wp_list.setCurrentRow(row - 1)
            self._recalc()

    def _wp_down(self):
        row = self.wp_list.currentRow()
        if 0 <= row < len(self.waypoints) - 1:
            self.waypoints[row + 1], self.waypoints[row] = self.waypoints[row], self.waypoints[row + 1]
            self._rebuild_wp_list()
            self.wp_list.setCurrentRow(row + 1)
            self._recalc()

    def _wp_clear(self):
        self.waypoints.clear()
        self._rebuild_wp_list()
        self._recalc()

    # ---------------------------------------------------------------- search
    def _do_search(self):
        if not self.universe:
            return
        self.search_results.clear()
        for s in self.universe.search(self.search.text()):
            it = QListWidgetItem(f"{s.name}  ({s.security:.1f})  ·  {s.region_id}")
            it.setData(_ROLE_SYS, s.id)
            self.search_results.addItem(it)

    def _add_from_search(self, item: QListWidgetItem):
        sid = item.data(_ROLE_SYS)
        if self.universe and sid in self.universe.systems:
            self._add_waypoint(self.universe.systems[sid])

    def _on_map_click(self, sid: int):
        if self.universe and sid in self.universe.systems:
            self._add_waypoint(self.universe.systems[sid])

    # ------------------------------------------------------------ auto-route
    def _auto_route(self):
        if not self.universe or len(self.waypoints) < 2:
            QMessageBox.information(self, "Auto-route",
                                    "Add at least an origin and a destination first.")
            return
        policy = self.cmb_dock.currentIndex()
        if policy != 0 and not self.universe.stations:
            # Need NPC station data first; load it, then retry.
            self.statusBar().showMessage("Loading station data for docking-aware routing...")
            worker = Worker(self.universe.load_stations)
            worker.finished_ok.connect(lambda _: (self.statusBar().clearMessage(),
                                                   self._auto_route()))
            worker.failed.connect(lambda m: QMessageBox.warning(self, "Station data", m))
            self._run(worker)
            return
        self._do_auto_route()

    def _do_auto_route(self):
        origin, dest = self.waypoints[0], self.waypoints[-1]
        ship, skills = self.current_ship(), self.current_skills()
        minimize = "jumps" if self.cmb_min.currentIndex() == 0 else "fuel"
        can_land = self._dock_predicate(ship)
        path = router.find_path(self.universe, ship, skills, origin, dest,
                                minimize=minimize, can_land=can_land)
        if not path:
            QMessageBox.warning(
                self, "Auto-route",
                "No jump path found within range under the current docking filter "
                "(destination may be high-sec, too far, or lack a dock your hull "
                "can use). Try a longer-range ship or relax the docking filter.")
            return
        self.waypoints = path
        self._rebuild_wp_list()
        self._recalc()

    def _dock_predicate(self, ship):
        """System -> bool: does an intermediate landing have a usable dock?"""
        policy = self.cmb_dock.currentIndex()
        if policy == 0:
            return None
        safe_only = policy == 2
        allowed: set[int] = set()
        for sys_id, stations in self.universe.system_stations.items():
            for st in stations:
                chk = docking.check_npc_station(ship, st.type_name, st.max_volume)
                if chk.can_dock and (chk.safe or not safe_only):
                    allowed.add(sys_id)
                    break
        for d in self._dockables:
            if d.kind == "structure":
                chk = docking.check_structure(ship, d.type_id, d.name, d.location_id)
                if chk.can_dock and (chk.safe or not safe_only):
                    allowed.add(d.solar_system_id)
        return lambda s: s.id in allowed

    # -------------------------------------------------------------- recalc
    def _recalc(self):
        ship = self.current_ship()
        skills = self.current_skills()
        rng = mechanics.max_range_ly(ship, skills)
        self.lbl_range.setText(f"{rng:.2f} ly")
        self.lbl_fuel.setText(f"{mechanics.fuel_per_ly(ship, skills):,.0f} iso")
        self.lbl_iso.setText(ship.isotope)
        self.lbl_bridge.setText(f"{ship.bridge_range_ly:.1f} ly" if ship.bridge_range_ly else "—")
        self.chk_bridge.setEnabled(bool(ship.bridge_range_ly))

        if not self.universe or not self.map_view:
            return

        plan = router.simulate(ship, skills, self.waypoints)
        self._fill_table(plan)

        reach_sys = self._reach_system()
        reach_rng = self.reach_range(ship, skills)
        reachable = (self.universe.within_range(reach_sys, reach_rng)
                     if reach_sys else [])
        in_range = [leg.in_range for leg in plan.legs]
        self.map_view.show_plan(
            reach_sys, self.waypoints, in_range, reach_rng,
            [s for s, _ in reachable],
        )
        if self._dockables:
            self._render_structs()

    def _fill_table(self, plan: router.RoutePlan):
        self.table.setRowCount(len(plan.legs))
        for i, leg in enumerate(plan.legs):
            vals = [
                leg.src.name, leg.dst.name, f"{leg.distance_ly:.2f}",
                f"{leg.fuel:,}", f"{leg.cooldown_min:.1f}m",
                f"{leg.fatigue_after_min:.1f}m", "✓" if leg.in_range else "✗",
            ]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if not leg.in_range:
                    it.setForeground(Qt.GlobalColor.red)
                self.table.setItem(i, c, it)
        if plan.legs:
            hrs = plan.total_time_min / 60.0
            warn = "" if plan.all_in_range else "  ⚠ some legs exceed jump range"
            self.totals.setText(
                f"{plan.jumps} jump(s) · {plan.total_fuel:,} isotopes · "
                f"travel time ≈ {plan.total_time_min:.0f} min ({hrs:.1f} h) · "
                f"peak fatigue {plan.peak_fatigue_min:.0f} min{warn}")
        else:
            self.totals.setText("Add 2+ waypoints to plan a route.")

    # ---------------------------------------------------------------- ESI
    def _refresh_login_label(self):
        if self.token:
            self.lbl_login.setText(f"Logged in: <b>{self.token.character_name}</b>")
            self.btn_login.setText("Re-authenticate")
        else:
            self.lbl_login.setText("Not logged in.")
            self.btn_login.setText("Log in with EVE")

    def _set_client_id(self):
        current = config.get_client_id() or ""
        text, ok = QInputDialog.getText(
            self, "EVE Client ID",
            "Create an application at https://developers.eveonline.com\n"
            f"with callback URL:  http://localhost:{config.CALLBACK_PORT}/callback\n"
            "and paste its Client ID here:",
            text=current)
        if ok and text.strip():
            cfg = config.load_config()
            cfg["client_id"] = text.strip()
            config.save_config(cfg)
            QMessageBox.information(self, "Saved", "Client ID saved.")

    def _set_scopes(self):
        current = " ".join(config.get_scopes())
        text, ok = QInputDialog.getMultiLineText(
            self, "ESI scopes",
            "Paste the JSON scope array from your EVE application, or a\n"
            "space/comma/newline-separated list. These must match scopes\n"
            "granted to your app (https://developers.eveonline.com).",
            current)
        if ok:
            scopes = config.parse_scopes(text)
            config.set_scopes(scopes)
            QMessageBox.information(self, "Saved", f"Requesting {len(scopes)} scope(s).")

    def _login(self):
        client_id = config.get_client_id()
        if not client_id:
            self._set_client_id()
            client_id = config.get_client_id()
            if not client_id:
                return
        self.btn_login.setEnabled(False)
        self.lbl_login.setText("Opening EVE SSO in your browser...")
        worker = Worker(auth.login, client_id)
        worker.finished_ok.connect(self._on_login)
        worker.failed.connect(self._on_login_fail)
        self._run(worker)

    def _on_login(self, token: auth.Token):
        self.token = token
        auth.save(token)
        self.esi = EsiClient(token, config.get_client_id())
        self.btn_login.setEnabled(True)
        self._refresh_login_label()

    def _on_login_fail(self, msg: str):
        self.btn_login.setEnabled(True)
        self._refresh_login_label()
        extra = ""
        low = msg.lower()
        if "redirect" in low or "invalid_request" in msg or "timed out" in low:
            extra = (
                "\n\nYour EVE application's Callback URL must be EXACTLY:\n"
                f"    {config.REDIRECT_URI}\n"
                "Edit it at https://developers.eveonline.com (note the /callback "
                "path and no trailing space), then log in again.")
        elif "invalid_scope" in msg:
            extra = (
                "\n\nA requested scope isn't granted to your EVE application.\n"
                "Fix it at https://developers.eveonline.com:\n"
                " • Make sure the application is not 'Authentication Only'.\n"
                " • Tick the scopes you need (e.g. esi-assets.read_assets.v1,\n"
                "   esi-universe.read_structures.v1, esi-location.read_location.v1).\n"
                " • Confirm the Client ID you entered belongs to that same app.\n"
                "Then use File → Set ESI scopes… to match, and log in again.")
        QMessageBox.warning(self, "Login failed", msg + extra)

    def _logout(self):
        auth.logout()
        self.token = None
        self.esi = None
        self.struct_list.clear()
        self._refresh_login_label()

    def _load_structures(self):
        if not self.token:
            QMessageBox.information(self, "Structures", "Log in first.")
            return
        if not self.esi:
            self.esi = EsiClient(self.token, config.get_client_id())
        self.btn_structs.setEnabled(False)
        self.struct_list.clear()
        self.struct_list.addItem("Loading assets and resolving locations...")
        worker = Worker(self.esi.dockable_locations)
        worker.finished_ok.connect(self._on_structures)
        worker.failed.connect(lambda m: (self.btn_structs.setEnabled(True),
                                         QMessageBox.warning(self, "Structures", m)))
        self._run(worker)

    def _on_structures(self, dockables):
        self.btn_structs.setEnabled(True)
        self._dockables = dockables
        self._render_structs()

    def _classify(self, d):
        """DockCheck for a Dockable given the current ship."""
        ship = self.current_ship()
        if d.kind == "station":
            tname = ""
            if self.universe:
                tname = self.universe.station_type_names.get(d.type_id, "")
            return docking.check_npc_station(ship, tname, d.max_volume)
        return docking.check_structure(ship, d.type_id, d.name, d.location_id)

    def _render_structs(self):
        self.struct_list.clear()
        if not self._dockables:
            self.struct_list.addItem("No stations/structures found in your assets.")
            return
        policy = self.cmb_dock.currentIndex()  # 0 none, 1 any, 2 safe-only
        shown = 0
        for d in self._dockables:
            chk = self._classify(d)
            if policy == 1 and not chk.can_dock:
                continue
            if policy == 2 and not (chk.can_dock and chk.safe):
                continue
            icon = {"ok": "✓", "risky": "⚠", "no docking": "✗"}[chk.status]
            it = QListWidgetItem(f"{icon} {d.name}  [{d.kind}] — {chk.note}")
            it.setData(_ROLE_SYS, d.solar_system_id)
            if chk.status == "no docking":
                it.setForeground(Qt.GlobalColor.gray)
            elif chk.status == "risky":
                it.setForeground(Qt.GlobalColor.yellow)
            self.struct_list.addItem(it)
            shown += 1
        if shown == 0:
            self.struct_list.addItem("(no locations match the docking filter)")

    def _add_from_struct(self, item: QListWidgetItem):
        sid = item.data(_ROLE_SYS)
        if self.universe and sid in self.universe.systems:
            self._add_waypoint(self.universe.systems[sid])
        else:
            QMessageBox.information(self, "Structure",
                                    "This location's solar system isn't in the map data.")
