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
        self._corp_alliance: dict[int, int | None] = {}
        self._owner_names: dict[int, str] = {}
        self._owners_pending = 0
        self._owners_done: set[int] = set()
        self.my_corp_id: int | None = None
        self.my_alliance_id: int | None = None
        self.incursion_systems: set[int] = set()
        self.avoided_ids: set[int] = set()
        self._ansiblex_pending: list = []
        self.docking_rights_ids: set[int] = set()
        self.sov_owners: dict[int, tuple] = {}
        self.sov_names: dict[int, str] = {}
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
        self._fetch_sovereignty()
        self._resolve_docking_rights()
        self._load_universe()

    def sov_of(self, system_id: int):
        """(owner_name, kind, standing, label) for a system's sov holder."""
        entry = self.sov_owners.get(system_id)
        if not entry:
            return None
        owner_id, kind = entry
        name = self.sov_names.get(owner_id, str(owner_id))
        if kind == "alliance":
            standing = self.standings.get(owner_id)
            label = "alliance contact" if standing is not None else ""
            if self.my_alliance_id and owner_id == self.my_alliance_id:
                standing, label = 10.0, "your alliance"
        else:
            standing, label = self.owner_relation(owner_id)
        return name, kind, standing, label

    def _fetch_sovereignty(self):
        from ..esi import client
        w = Worker(client.sovereignty)
        w.finished_ok.connect(self._on_sovereignty)
        w.failed.connect(lambda m: None)
        self._run(w)

    def _on_sovereignty(self, result):
        result = result or {}
        self.sov_owners = result.get("owners", {})
        self.sov_names = result.get("names", {})
        if self.sov_owners:
            self.statusBar().showMessage(
                f"Sovereignty loaded for {len(self.sov_owners)} systems.", 5000)
        if self.map_view:
            self.map_view.set_sov_lookup(self.sov_label)

    def sov_label(self, system_id: int):
        """Short owner label for map hover, e.g. 'Goonswarm Federation'."""
        info = self.sov_of(system_id)
        return info[0] if info else None

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

    def has_docking_rights(self, owner_id: int) -> bool:
        """True if the owning corp, or its alliance, is on your rights list."""
        if not owner_id or not self.docking_rights_ids:
            return False
        if owner_id in self.docking_rights_ids:
            return True
        alliance_id = self._corp_alliance.get(owner_id)
        return bool(alliance_id and alliance_id in self.docking_rights_ids)

    def _resolve_docking_rights(self, names=None, on_done=None):
        """Resolve configured corp/alliance names to IDs via ESI (public)."""
        from ..esi import client
        names = names if names is not None else config.get_docking_rights()
        if not names:
            self.docking_rights_ids = set()
            if on_done:
                on_done([])
            return
        w = Worker(client.resolve_ids, names)

        def done(result):
            result = result or {}
            self.docking_rights_ids = {i for i, _ in result.get("ids", {}).values()}
            if on_done:
                on_done(result.get("unknown", []))
            self.route.refresh()
            self._render_character()

        w.finished_ok.connect(done)
        w.failed.connect(lambda m: on_done([m]) if on_done else None)
        self._run(w)

    def _edit_docking_rights(self):
        from .dialogs import DockingRightsDialog
        dlg = DockingRightsDialog(self, config.get_docking_rights())
        if not dlg.exec():
            return
        names = dlg.names()
        config.set_docking_rights(names)

        def report(unknown):
            msg = f"{len(names) - len(unknown)} of {len(names)} name(s) resolved."
            if unknown:
                msg += "\nNot found: " + ", ".join(unknown[:6])
            QMessageBox.information(self, "Docking rights", msg)

        self._resolve_docking_rights(names, report)

    def hostile_threshold(self) -> float:
        return 0.0  # standing below this counts as hostile

    def standing_of(self, entity_id: int):
        """Standing toward an entity; falls back to its alliance's standing
        (structure owners are corps, but standings are often set alliance-wide)."""
        if not entity_id:
            return None
        if entity_id in self.standings:
            return self.standings[entity_id]
        alliance_id = self._corp_alliance.get(entity_id)
        if alliance_id and alliance_id in self.standings:
            return self.standings[alliance_id]
        return None

    def owner_relation(self, owner_id: int, alliance_id=None):
        """(standing, label) for a structure owner.

        You never have a contact entry for your OWN corp/alliance, so those are
        reported explicitly instead of looking like "no standing".
        """
        if not owner_id:
            return None, ""
        if self.my_corp_id and owner_id == self.my_corp_id:
            return 10.0, "your corporation"
        if self.my_alliance_id and alliance_id and alliance_id == self.my_alliance_id:
            return 10.0, "your alliance"
        if owner_id in self.standings:
            return self.standings[owner_id], "corp contact"
        if alliance_id and alliance_id in self.standings:
            return self.standings[alliance_id], "alliance contact"
        return None, ""

    def owner_relation_cached(self, owner_id: int):
        """Synchronous (standing, label) for list rendering.

        Uses the cached corp->alliance map; if an owner hasn't been resolved
        yet it kicks off a background lookup and refreshes the list when done,
        so alliance-based standings appear without blocking the UI.
        """
        if not owner_id:
            return None, ""
        if owner_id not in self._corp_alliance and self.esi:
            self._prefetch_owner(owner_id)
        return self.owner_relation(owner_id, self._corp_alliance.get(owner_id))

    def _prefetch_owner(self, owner_id: int):
        self._corp_alliance[owner_id] = None          # in-flight marker
        self._owners_pending += 1
        w = Worker(self.esi.owner_details, owner_id)

        def done(d):
            self._corp_alliance[owner_id] = (d or {}).get("alliance_id")
            self._owner_names[owner_id] = (d or {}).get("name", "")
            self._owners_done.add(owner_id)
            self._owner_resolved()

        w.finished_ok.connect(done)
        w.failed.connect(lambda m: (self._owners_done.add(owner_id),
                                    self._owner_resolved()))
        self._run(w)

    def _owner_resolved(self):
        """Refresh once the whole batch of owner lookups has landed."""
        self._owners_pending = max(0, self._owners_pending - 1)
        if self._owners_pending == 0:
            self._apply_ansiblex_pending()   # owners known -> adopt own gates
            self.route.refresh()             # re-rank with new info

    def request_owner_details(self, owner_id: int, callback):
        """Resolve owner corp name + alliance, then hand back (details, standing,
        label) so a dialog can fill itself in asynchronously."""
        if not owner_id or not self.esi:
            return
        w = Worker(self.esi.owner_details, owner_id)

        def done(d):
            d = d or {}
            alliance_id = d.get("alliance_id")
            self._corp_alliance[owner_id] = alliance_id
            standing, label = self.owner_relation(owner_id, alliance_id)
            callback(d, standing, label)

        w.finished_ok.connect(done)
        w.failed.connect(lambda m: None)
        self._run(w)

    def _fetch_contacts(self):
        if not self.token:
            return
        if not self.esi:
            self.esi = EsiClient(self.token, config.get_client_id())
        w = Worker(self.esi.contacts)
        w.finished_ok.connect(self._on_contacts)
        w.failed.connect(
            lambda m: self.statusBar().showMessage(f"Contacts unavailable: {m}", 10000))
        self._run(w)

    def _on_contacts(self, result: dict):
        result = result or {}
        self.standings = result.get("standings", {})
        self.my_corp_id = result.get("corp_id")
        self.my_alliance_id = result.get("alliance_id")
        errors = result.get("errors", [])
        if self.standings:
            msg = f"Loaded {len(self.standings)} contact standing(s)."
            if errors:
                msg += f"  ({len(errors)} list(s) unavailable - check scopes)"
        elif errors:
            msg = ("No contacts loaded - missing scopes? Need "
                   "esi-characters/corporations/alliances.read_contacts. "
                   + "; ".join(errors[:2]))
        else:
            msg = "No contacts found on your character, corp or alliance lists."
        self.statusBar().showMessage(msg, 12000)
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
        # Ansiblex gates carry their whole link in the name ("A » B"), so any
        # we stumble across while listing a system's structures is free
        # network discovery.
        self._absorb_ansiblex(found)

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
            ("Ansiblex jump gates...", self._edit_bridges),
            ("Docking rights...", self._edit_docking_rights),
            ("Avoided systems...", self._edit_avoided),
            ("Reload map data", self._reload_map),
            ("Log out", self._logout),
            ("Quit", self.close),
        ):
            a = QAction(text, self)
            a.triggered.connect(slot)
            m.addAction(a)

        view = self.menuBar().addMenu("&View")
        self.act_gate_links = QAction("Show stargate links", self, checkable=True)
        self.act_gate_links.setChecked(True)
        self.act_gate_links.toggled.connect(self._toggle_gate_links)
        view.addAction(self.act_gate_links)

    def _toggle_gate_links(self, visible: bool):
        if self.map_view:
            self.map_view.set_gate_links_visible(visible)
        self._save_settings()

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
        self.route.gate_assist_requested.connect(self._gate_assist)
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
        view = s.get("view", {})
        self.act_gate_links.blockSignals(True)
        self.act_gate_links.setChecked(bool(view.get("gate_links", True)))
        self.act_gate_links.blockSignals(False)

    def _save_settings(self):
        if not self._built:
            return
        config.save_settings({
            "ship": self.ship.state(),
            "route": self.route.state(),
            "view": {"gate_links": self.act_gate_links.isChecked()},
        })

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
        self.map_view.set_gate_links_visible(self.act_gate_links.isChecked())
        universe.set_bridges(config.get_bridges())
        self.map_view.refresh_bridges()
        self.avoided_ids = {s.id for s in
                            (universe.by_name(n) for n in config.get_avoided())
                            if s is not None}
        self.map_view.set_avoided(self.avoided_ids)
        if self.sov_owners:
            self.map_view.set_sov_lookup(self.sov_label)
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
        act_avoid = menu.addAction(
            "Stop avoiding this system" if self.is_avoided(sid)
            else "Avoid this system")
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
        elif chosen == act_avoid:
            self.toggle_avoid(sid)
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
                   jump_cost=self.route.jump_cost(),
                   use_ansiblex=self.route.use_ansiblex(),
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
        """Systems routing must never pass through: manual avoids + incursions."""
        avoid = set(self.avoided_ids)
        if self.route.avoid_incursions():
            avoid |= self.incursion_systems
        return avoid

    def is_avoided(self, system_id: int) -> bool:
        return system_id in self.avoided_ids

    def toggle_avoid(self, system_id: int):
        if not self.universe or system_id not in self.universe.systems:
            return
        name = self.universe.systems[system_id].name
        if system_id in self.avoided_ids:
            self.avoided_ids.discard(system_id)
            msg = f"{name} is no longer avoided."
        else:
            self.avoided_ids.add(system_id)
            msg = f"Avoiding {name} in all route planning."
        config.set_avoided(
            [self.universe.systems[i].name for i in self.avoided_ids
             if i in self.universe.systems])
        self.statusBar().showMessage(msg, 5000)
        if self.map_view:
            self.map_view.set_avoided(self.avoided_ids)
        self._recalc()

    def _edit_avoided(self):
        from .dialogs import AvoidDialog
        names = sorted(self.universe.systems[i].name for i in self.avoided_ids
                       if i in self.universe.systems) if self.universe else \
            config.get_avoided()
        dlg = AvoidDialog(self, names)
        if not dlg.exec():
            return
        wanted = dlg.names()
        ids, bad = set(), []
        for n in wanted:
            s = self.universe.by_name(n) if self.universe else None
            (ids.add(s.id) if s else bad.append(n))
        self.avoided_ids = ids
        config.set_avoided([self.universe.systems[i].name for i in ids])
        if self.map_view:
            self.map_view.set_avoided(ids)
        msg = f"Avoiding {len(ids)} system(s)."
        if bad:
            msg += "\nUnknown: " + ", ".join(bad)
        QMessageBox.information(self, "Avoided systems", msg)
        self._recalc()

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
        from .dialogs import EsiSetupDialog
        dlg = EsiSetupDialog(self, config.get_client_id() or "",
                             config.REDIRECT_URI, config.get_scopes())
        if dlg.exec() and dlg.client_id():
            cfg = config.load_config()
            cfg["client_id"] = dlg.client_id()
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

    def _edit_bridges(self):
        from .dialogs import AnsiblexDialog
        dlg = AnsiblexDialog(self, config.get_bridges())
        dlg.btn_esi.clicked.connect(lambda: self._load_ansiblex_esi(dlg))
        dlg.btn_search.clicked.connect(lambda: self._search_ansiblex(dlg))
        dlg.search_field.returnPressed.connect(lambda: self._search_ansiblex(dlg))
        if not dlg.exec():
            return
        pairs = dlg.pairs()
        if self.universe:
            resolved = self.universe.set_bridges(pairs)
            bad = len(pairs) - len(resolved)
            config.set_bridges(resolved)
            self.map_view.refresh_bridges() if self.map_view else None
            msg = f"{len(resolved)} Ansiblex link(s) active."
            if bad:
                msg += f"\n{bad} line(s) ignored - unknown system name."
            QMessageBox.information(self, "Ansiblex jump gates", msg)
            self._recalc()
        else:
            config.set_bridges(pairs)

    def _absorb_ansiblex(self, structures: list) -> int:
        """Queue any Ansiblex gates found in ``structures`` for adoption.

        Only gates owned by your own corporation or alliance are usable, so
        candidates wait here until the owner's alliance has been resolved.
        """
        from ..esi import client
        if not self.universe:
            return 0
        for d in structures:
            if getattr(d, "type_id", 0) != client.ANSIBLEX_TYPE_ID:
                continue
            parsed = client.parse_ansiblex_name(d.name)
            if parsed:
                self._ansiblex_pending.append((getattr(d, "owner_id", 0), parsed))
        return self._apply_ansiblex_pending()

    def _apply_ansiblex_pending(self) -> int:
        """Adopt queued Ansiblex links whose owner is your corp or alliance."""
        if not self._ansiblex_pending or not self.universe:
            return 0
        known = {tuple(sorted(p)) for p in config.get_bridges()}
        new_pairs, still_pending = [], []
        for owner_id, (a_raw, b_raw) in self._ansiblex_pending:
            _, label = self.owner_relation_cached(owner_id)
            if label not in ("your corporation", "your alliance"):
                # Owner not resolved yet? Keep waiting for the alliance lookup
                # (in-flight and "has no alliance" both cache as None, so track
                # completion separately).
                if owner_id and owner_id not in self._owners_done:
                    still_pending.append((owner_id, (a_raw, b_raw)))
                continue
            a = self.universe.match_system(a_raw)
            b = self.universe.match_system(b_raw)
            if not a or not b or tuple(sorted((a.name, b.name))) in known:
                continue
            known.add(tuple(sorted((a.name, b.name))))
            new_pairs.append([a.name, b.name])
        self._ansiblex_pending = still_pending
        if not new_pairs:
            return 0
        resolved = self.universe.set_bridges(config.get_bridges() + new_pairs)
        config.set_bridges(resolved)
        if self.map_view:
            self.map_view.refresh_bridges()
        self.statusBar().showMessage(
            f"Adopted {len(new_pairs)} corp/alliance Ansiblex link(s).", 6000)
        self._recalc()
        return len(new_pairs)

    def _load_ansiblex_esi(self, dlg):
        if not self.token:
            dlg.status.setText("Log in with EVE first.")
            return
        if not self.esi:
            self.esi = EsiClient(self.token, config.get_client_id())
        dlg.btn_esi.setEnabled(False)
        dlg.status.setText("Querying ESI…")
        w = Worker(self.esi.ansiblex_links)
        w.progress.connect(dlg.status.setText)

        def done(result):
            dlg.btn_esi.setEnabled(True)
            result = result or {}
            dlg.merge_links(result.get("links", []), result.get("errors"))

        w.finished_ok.connect(done)
        w.failed.connect(lambda m: (dlg.btn_esi.setEnabled(True),
                                    dlg.status.setText(m)))
        self._run(w)

    def _search_ansiblex(self, dlg):
        """Find Ansiblex gates in one system (needs only the search scope)."""
        name = dlg.search_field.text().strip()
        if not name:
            return
        if not self.token:
            dlg.status.setText("Log in with EVE first.")
            return
        sys_obj = self.universe.match_system(name) if self.universe else None
        if not sys_obj:
            dlg.status.setText(f"Unknown system: {name}")
            return
        if not self.esi:
            self.esi = EsiClient(self.token, config.get_client_id())
        dlg.btn_search.setEnabled(False)
        dlg.status.setText(f"Searching {sys_obj.name}…")
        w = Worker(self.esi.ansiblex_in_system, sys_obj.name, sys_obj.id)

        def done(links):
            dlg.btn_search.setEnabled(True)
            if links:
                dlg.merge_links(links)
            else:
                dlg.status.setText(
                    f"No Ansiblex gates visible in {sys_obj.name}. (Search only "
                    "returns structures your character has access to.)")

        w.finished_ok.connect(done)
        w.failed.connect(lambda m: (dlg.btn_search.setEnabled(True),
                                    dlg.status.setText(m)))
        self._run(w)

    def _gate_assist(self):
        if not self.universe or len(self.route.waypoints) < 2:
            QMessageBox.information(self, "Gate assist",
                                    "Add an origin and a destination first.")
            return
        ship = self.ship.current_ship()
        skills = self.ship.current_skills()
        origin = self.route.waypoints[0].system
        dest = self.route.waypoints[-1].system
        self.route.set_busy(True)
        w = Worker(router.analyze_gate_assist, self.universe, ship, skills,
                   origin, dest, gate_pref=self.route.gate_pref(),
                   jump_cost=self.route.jump_cost(),
                   use_ansiblex=self.route.use_ansiblex(),
                   can_land=self._dock_predicate(ship), avoid=self.avoid_systems(),
                   strategy=self.route.strategy())
        w.finished_ok.connect(lambda res: (self.route.set_busy(False),
                                           self._show_gate_assist(origin, dest, res)))
        w.failed.connect(lambda m: (self.route.set_busy(False),
                                    QMessageBox.warning(self, "Gate assist", m)))
        self._run(w)

    def _show_gate_assist(self, origin, dest, analysis):
        from .dialogs import GateAssistDialog
        GateAssistDialog(self, origin.name, dest.name, analysis or {}).exec()

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
