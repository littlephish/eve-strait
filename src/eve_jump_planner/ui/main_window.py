"""Main window: owns shared state (universe, ESI) and coordinates the panels."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDockWidget,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
)

from .. import config
from ..data.universe import Universe
from ..esi import auth, images
from ..esi.client import EsiClient
from ..jump import router
from .map_view import MapView
from .panels.character_panel import CharacterPanel
from .panels.route_panel import RoutePanel
from .panels.ship_panel import ShipSkillsPanel
from .workers import Worker


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EVE Jump Planner")
        self.resize(1500, 950)

        self.universe: Universe | None = None
        self.map_view: MapView | None = None
        self.dockables: list = []
        self.token: auth.Token | None = auth.load_saved()
        self.esi: EsiClient | None = None
        self._workers: list[Worker] = []
        self._structs_fetched: set[int] = set()
        self.standings: dict[int, float] = {}
        self.incursion_systems: set[int] = set()
        self._built = False

        self._status = QLabel("Loading New Eden map data...")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCentralWidget(self._status)

        self._build_menu()
        self._build_docks()
        self._load_settings()
        self._wire()
        self._built = True

        self.character.set_login(self.token.character_name if self.token else None)
        self._load_cached_dockables()
        if self.token:
            self._fetch_contacts()
        self._fetch_incursions()
        self._load_universe()

    def _fetch_incursions(self):
        from ..esi import client
        w = Worker(client.incursions)
        w.finished_ok.connect(lambda s: setattr(self, "incursion_systems", s or set()))
        w.failed.connect(lambda m: None)
        self._run(w)

    def _load_cached_dockables(self):
        """Show the character's last-fetched dockables immediately (cached file)."""
        if not self.token:
            return
        from ..esi import client
        cached = client.load_dockables(self.token.character_id)
        if cached:
            self.dockables = cached
            self.character.set_dockables(cached)

    # -- context interface used by panels ----------------------------------
    def current_ship(self):
        return self.ship.current_ship()

    def current_skills(self):
        return self.ship.current_skills()

    def hostile_threshold(self) -> float:
        return 0.0  # standing below this counts as hostile

    def standing_of(self, entity_id: int):
        return self.standings.get(entity_id)

    def _fetch_contacts(self):
        if not self.token:
            return
        if not self.esi:
            self.esi = EsiClient(self.token, config.get_client_id())
        w = Worker(self.esi.contacts)
        w.finished_ok.connect(self._on_contacts)
        w.failed.connect(lambda m: None)  # silent (scope may be missing)
        self._run(w)

    def _on_contacts(self, standings: dict):
        self.standings = standings or {}
        self.route.refresh()

    def request_station_image(self, type_id: int, callback):
        worker = Worker(images.render_bytes, type_id)
        worker.finished_ok.connect(callback)
        self._run(worker)

    def request_entity_name(self, entity_id: int, callback):
        from ..esi import client
        worker = Worker(client.resolve_names, [entity_id])
        worker.finished_ok.connect(lambda d: callback(d.get(entity_id)))
        worker.failed.connect(lambda m: None)
        self._run(worker)

    def ensure_public_structures(self, system):
        """Fetch publicly-dockable player structures in a system (once), so the
        dock list matches the in-game search. No-op without login/search scope."""
        if system is None or not self.token or system.id in self._structs_fetched:
            return
        self._structs_fetched.add(system.id)
        if not self.esi:
            self.esi = EsiClient(self.token, config.get_client_id())
        w = Worker(self.esi.structures_in_system, system.name, system.id)
        w.finished_ok.connect(self._merge_public_structures)
        w.failed.connect(lambda m: None)  # silent (scope may be missing)
        self._run(w)

    def _merge_public_structures(self, found: list):
        existing = {d.location_id for d in self.dockables}
        added = [d for d in found if d.location_id not in existing]
        if not added:
            return
        self.dockables.extend(added)
        if self.token:
            from ..esi import client
            client.save_dockables(self.token.character_id, self.dockables)
        self.route.refresh()
        self._render_character()

    # -- construction -------------------------------------------------------
    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        for text, slot in (
            ("Set EVE Client ID...", self._set_client_id),
            ("Set ESI scopes...", self._set_scopes),
            ("Reload map data", self._reload_map),
            ("Log out", self._logout),
            ("Quit", self.close),
        ):
            a = QAction(text, self)
            a.triggered.connect(slot)
            m.addAction(a)

    def _build_docks(self):
        self.ship = ShipSkillsPanel()
        self.route = RoutePanel(self)
        self.character = CharacterPanel()

        d_char = QDockWidget("Character & Structures", self)
        d_char.setWidget(self.character)
        d_ship = QDockWidget("Ship & Skills", self)
        d_ship.setWidget(self.ship)
        d_route = QDockWidget("Route", self)
        d_route.setWidget(self.route)

        # Left column: character (top) with ship config below it (bottom-left).
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, d_char)
        self.splitDockWidget(d_char, d_ship, Qt.Orientation.Vertical)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, d_route)

    def _wire(self):
        self.ship.changed.connect(self._on_ship_changed)
        self.route.changed.connect(self._on_route_changed)
        self.route.autoroute_requested.connect(self._auto_route)
        self.character.login_requested.connect(self._login)
        self.character.load_structures_requested.connect(self._load_structures)
        self.character.add_system.connect(self.route.add_system)

    # -- settings persistence ----------------------------------------------
    def _load_settings(self):
        s = config.get_settings()
        if s.get("ship"):
            self.ship.restore(s["ship"])
        if s.get("route"):
            self.route.restore(s["route"])

    def _save_settings(self):
        if not self._built:
            return
        config.save_settings({"ship": self.ship.state(), "route": self.route.state()})

    # -- universe / stations ------------------------------------------------
    def _load_universe(self):
        w = Worker(Universe.load)
        w.progress.connect(self._status.setText)
        w.finished_ok.connect(self._on_universe)
        w.failed.connect(lambda m: self._status.setText(
            f"Failed to load map data:\n{m}\n\nUse File → Reload map data."))
        self._run(w)

    def _on_universe(self, universe: Universe):
        self.universe = universe
        self.map_view = MapView(universe)
        self.map_view.system_clicked.connect(self._on_map_click)
        self.map_view.system_context.connect(self._map_context)
        self.setCentralWidget(self.map_view)
        self._recalc()
        # Load station data in the background so dock names/photos work.
        self.statusBar().showMessage("Loading station data...")
        w = Worker(universe.load_stations)
        w.finished_ok.connect(self._on_stations)
        w.failed.connect(lambda m: self.statusBar().showMessage(f"Station data: {m}", 8000))
        self._run(w)

    def _on_stations(self, _):
        self.statusBar().showMessage("Station data ready.", 4000)
        self.route.refresh()
        self._render_character()

    def _reload_map(self):
        for p in (config.SDE_CSV_PATH, config.MAP_REGIONS_PATH, config.MAP_JUMPS_PATH):
            p.unlink(missing_ok=True)
        self._status = QLabel("Reloading map data...")
        self.setCentralWidget(self._status)
        self._load_universe()

    # -- recalc coordinator -------------------------------------------------
    def _on_ship_changed(self):
        self._save_settings()
        if self.universe:
            self.route.refresh()      # dock availability changed with hull
        self._render_character()
        self._recalc()

    def _on_route_changed(self):
        self._save_settings()
        self._render_character()
        self._recalc()

    def _recalc(self):
        if not self.universe or not self.map_view:
            return
        ship = self.ship.current_ship()
        skills = self.ship.current_skills()
        wps = self.route.systems()
        modes = self.route.modes()
        plan = router.simulate(ship, skills, wps, modes, self.route.strategy())
        self.route.display_plan(plan)

        reach_sys = self.route.selected_system()
        reach_rng = self.ship.reach_range()
        reachable = self.universe.within_range(reach_sys, reach_rng) if reach_sys else []
        in_range = [leg.in_range for leg in plan.legs]
        self.map_view.show_plan(reach_sys, wps, modes, in_range, reach_rng,
                                [s for s, _ in reachable])

    def _render_character(self):
        if not self.universe:
            return
        self.character.set_filter(self.route.policy())
        self.character.render(self.ship.current_ship(),
                              self.universe.station_type_names.get)

    def _on_map_click(self, sid: int):
        # Left-click only *selects* an existing waypoint (drives the range
        # circle). Adding waypoints is right-click → Add as waypoint.
        self.route.select_system(sid)

    def _map_context(self, sid: int):
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        act_add = menu.addAction("Add as waypoint")
        act_sysinfo = menu.addAction("Show system info")
        act_info = menu.addAction("Show station info")
        act_wp = menu.addAction("Set in-game destination")
        is_wp = any(wp.system.id == sid for wp in self.route.waypoints)
        act_remove = None
        if is_wp:
            menu.addSeparator()
            act_remove = menu.addAction("Remove waypoint")
        chosen = menu.exec(QCursor.pos())
        if chosen == act_add:
            self.route.add_system(sid)
        elif chosen == act_sysinfo:
            self.route.show_system_info(sid)
        elif chosen == act_info:
            self.route.show_station_info(sid)
        elif chosen == act_wp:
            self.set_ingame_waypoint(sid)
        elif act_remove is not None and chosen == act_remove:
            self.route.remove_system(sid)

    def set_ingame_waypoint(self, system_id: int):
        if not self.token:
            QMessageBox.information(
                self, "In-game waypoint",
                "Log in with EVE first (needs the esi-ui.write_waypoint.v1 scope).")
            return
        if not self.esi:
            self.esi = EsiClient(self.token, config.get_client_id())
        name = self.universe.systems[system_id].name if self.universe else system_id
        w = Worker(self.esi.set_waypoint, system_id)
        w.finished_ok.connect(
            lambda _: self.statusBar().showMessage(f"Set in-game destination: {name}", 5000))
        w.failed.connect(lambda m: QMessageBox.warning(self, "In-game waypoint", m))
        self._run(w)

    # -- auto-route ---------------------------------------------------------
    def _auto_route(self):
        if not self.universe or len(self.route.waypoints) < 2:
            QMessageBox.information(self, "Auto-route",
                                    "Add an origin and a destination first.")
            return
        ship = self.ship.current_ship()
        dest = self.route.waypoints[-1].system
        from ..data import docking
        if dest.security >= 0.5 and not docking.can_use_highsec_gates(ship):
            QMessageBox.warning(
                self, "Auto-route",
                f"{dest.name} is high-sec. Capitals cannot enter high-sec "
                "(no high-sec gates, and jump drives can't activate into high-sec). "
                "Only jump freighters can gate the final high-sec leg. Pick a "
                "low/null staging system instead.")
            return
        if self.route.policy() != 0 and not self.universe.stations:
            self.statusBar().showMessage("Loading station data for docking-aware routing...")
            w = Worker(self.universe.load_stations)
            w.finished_ok.connect(lambda _: (self._on_stations(_), self._do_auto_route()))
            w.failed.connect(lambda m: QMessageBox.warning(self, "Station data", m))
            self._run(w)
            return
        self._do_auto_route()

    def _do_auto_route(self):
        """Fill the gaps BETWEEN the user's waypoints (keeping them all as
        required stops), off the UI thread with a spinner."""
        ship = self.ship.current_ship()
        skills = self.ship.current_skills()
        systems = [wp.system for wp in self.route.waypoints]
        self.route.set_busy(True)
        w = Worker(router.route_through, self.universe, ship, skills, systems,
                   minimize=self.route.minimize(), gate_pref=self.route.gate_pref(),
                   can_land=self._dock_predicate(ship), avoid=self.avoid_systems())
        w.finished_ok.connect(lambda res: (self.route.set_busy(False),
                                           self._apply_auto_route(res)))
        w.failed.connect(lambda m: (self.route.set_busy(False),
                                    QMessageBox.warning(self, "Route", m)))
        self._run(w)

    def _apply_auto_route(self, result):
        if not result:
            QMessageBox.warning(
                self, "Auto-route",
                "Couldn't bridge one of your legs within range under the current "
                "filters (a leg may cross into high-sec for a capital, or need a "
                "closer staging system). Your waypoints are unchanged.")
            return
        systems, modes = result
        self.route.set_route(systems, modes)

    def avoid_systems(self) -> set:
        return self.incursion_systems if self.route.avoid_incursions() else set()

    def _dock_predicate(self, ship):
        policy = self.route.policy()
        if policy == 0:
            return None
        from ..data import docking
        safe_only = policy == 2
        allowed: set[int] = set()
        for sid, stations in self.universe.system_stations.items():
            for st in stations:
                chk = docking.check_npc_station(ship, st.type_name, st.max_volume)
                if chk.can_dock and (chk.safe or not safe_only):
                    allowed.add(sid)
                    break
        for d in self.dockables:
            if d.kind == "structure":
                chk = docking.check_structure(ship, d.type_id, d.name, d.location_id)
                if chk.can_dock and (chk.safe or not safe_only):
                    allowed.add(d.solar_system_id)
        return lambda s: s.id in allowed

    # -- ESI ----------------------------------------------------------------
    def _set_client_id(self):
        current = config.get_client_id() or ""
        text, ok = QInputDialog.getText(
            self, "EVE Client ID",
            "Create an application at https://developers.eveonline.com\n"
            f"with Callback URL EXACTLY:  {config.REDIRECT_URI}\n"
            "and paste its Client ID here:", text=current)
        if ok and text.strip():
            cfg = config.load_config()
            cfg["client_id"] = text.strip()
            config.save_config(cfg)
            QMessageBox.information(self, "Saved", "Client ID saved.")

    def _set_scopes(self):
        text, ok = QInputDialog.getMultiLineText(
            self, "ESI scopes",
            "Paste the JSON scope array from your EVE application, or a\n"
            "space/comma/newline-separated list.",
            " ".join(config.get_scopes()))
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
        self.character.btn_login.setEnabled(False)
        self.character.lbl_login.setText("Opening EVE SSO in your browser...")
        w = Worker(auth.login, client_id)
        w.finished_ok.connect(self._on_login)
        w.failed.connect(self._on_login_fail)
        self._run(w)

    def _on_login(self, token: auth.Token):
        self.token = token
        auth.save(token)
        self.esi = EsiClient(token, config.get_client_id())
        self.character.btn_login.setEnabled(True)
        self.character.set_login(token.character_name)
        self._fetch_contacts()

    def _on_login_fail(self, msg: str):
        self.character.btn_login.setEnabled(True)
        self.character.set_login(self.token.character_name if self.token else None)
        extra = ""
        low = msg.lower()
        if "redirect" in low or "invalid_request" in msg or "timed out" in low:
            extra = ("\n\nYour app's Callback URL must be EXACTLY:\n"
                     f"    {config.REDIRECT_URI}\n"
                     "Edit it at https://developers.eveonline.com, then retry.")
        elif "invalid_scope" in msg:
            extra = ("\n\nA requested scope isn't granted to your app. Enable the "
                     "scopes (not 'Authentication Only'), confirm the Client ID "
                     "matches, then File → Set ESI scopes… and retry.")
        QMessageBox.warning(self, "Login failed", msg + extra)

    def _logout(self):
        auth.logout()
        self.token = None
        self.esi = None
        self.dockables = []
        self.character.set_dockables([])
        self.character.set_login(None)
        self._render_character()

    def _load_structures(self):
        if not self.token:
            QMessageBox.information(self, "Structures", "Log in first.")
            return
        if not self.esi:
            self.esi = EsiClient(self.token, config.get_client_id())
        self.character.set_loading(True, "Loading assets…")
        w = Worker(self.esi.dockable_locations)
        w.progress.connect(lambda msg: self.character.set_loading(True, msg))
        w.finished_ok.connect(self._on_structures)
        w.failed.connect(lambda m: (self.character.set_loading(False),
                                    QMessageBox.warning(self, "Structures", m)))
        self._run(w)

    def _on_structures(self, dockables):
        self.character.set_loading(False)
        self.dockables = dockables
        self.character.set_dockables(dockables)
        if self.token:
            from ..esi import client
            client.save_dockables(self.token.character_id, dockables)
        self._render_character()
        self.route.refresh()

    # -- worker helper ------------------------------------------------------
    def _run(self, worker: Worker):
        self._workers.append(worker)
        worker.finished.connect(
            lambda: self._workers.remove(worker) if worker in self._workers else None)
        worker.start()
