"""Main window: owns shared state (universe, ESI) and coordinates the panels."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup
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
from .tasks import BusyIndicator, TaskRegistry
from .workers import Worker


def _scrollable(panel):
    """Wrap a panel so its dock can be narrowed.

    A dock cannot shrink below its content's minimum size hint, and these
    panels (tables, combo boxes, wrapped labels) ask for a lot. Inside a
    scroll area they can be squeezed and simply scroll instead, which is what
    lets the map keep the width.
    """
    from PySide6.QtWidgets import QFrame, QScrollArea

    area = QScrollArea()
    area.setWidget(panel)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setMinimumWidth(0)
    # Cap the side panels so they cannot crowd the map out; anything taller or
    # wider than this scrolls.
    area.setMaximumWidth(480)
    return area


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        from .. import __version__
        # Version in the title bar: it is the one piece of chrome that is
        # always on screen and always in a screenshot, so bug reports carry
        # the build with them without anyone having to ask.
        self.setWindowTitle(f"Eve-Strait {__version__}")
        self.resize(1500, 950)

        # Qt gives the bottom corners to the left/right dock areas by default,
        # which would let the character and route columns pinch a bottom dock
        # down to whatever gap is left between them. Reassigning both corners
        # to the bottom area is what makes a bottom dock run truly edge to
        # edge -- under the side columns, not just between them.
        self.setCorner(Qt.Corner.BottomLeftCorner,
                       Qt.DockWidgetArea.BottomDockWidgetArea)
        self.setCorner(Qt.Corner.BottomRightCorner,
                       Qt.DockWidgetArea.BottomDockWidgetArea)

        self.universe: Universe | None = None
        self.map_view: MapView | None = None
        self.dockables: list = []
        self.tokens: dict[int, auth.Token] = auth.load_all()
        self.cyno_alts: list = []
        self.token: auth.Token | None = auth.load_saved()
        self.esi: EsiClient | None = None
        self.location_system_id: int | None = None
        self.kill_activity: dict[int, dict] = {}
        self.jump_activity: dict[int, int] = {}
        self.activity_totals: dict = {"jumps": {}, "kills": {}, "hours": 0}
        self.sov_defense: dict[int, dict] = {}
        self.industry_index: dict[int, dict] = {}
        from ..esi import zkill
        self._cyno_sweep = zkill.load_sweep()
        self.cyno_activity = zkill.sweep_systems(self._cyno_sweep)
        self._cyno_worker = None
        self._cyno_stop = False
        # Key for the auto-waypoint's fire-once guard: (route systems tuple,
        # trigger system id). None means "nothing has fired yet".
        self._auto_waypoint_fired_for = None
        # Waypoints restore once, on the very first universe load. _on_universe
        # also fires on a manual "Reload map data", which must never stomp on
        # a route someone is actively editing.
        self._restored_waypoints_once = False
        self.chat_dock = None
        self.chat = None
        self.agent = None
        self.bridge = None
        self._workers: list[Worker] = []
        self._tasks = TaskRegistry()
        self._force_intel_outstanding = 0
        self._busy = None          # created with the status bar
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
        self.starbase_systems: dict[int, int] = {}
        self.sov_owners: dict[int, tuple] = {}
        self._wanderer_data: dict = {}
        self._hole_data: dict = {}
        self.sov_names: dict[int, str] = {}
        self._built = False

        self._status = QLabel("Loading New Eden map data...")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._build_menu()
        # Right-hand end of the status bar: showMessage() owns the left.
        self._busy = BusyIndicator(self)
        self.statusBar().addPermanentWidget(self._busy)
        self._sync_busy()
        self._build_docks()
        self._load_settings()
        self._wire()
        self._built = True
        self._sync_chat_panel()      # always a no-op now; see its docstring
        self._sync_bridge()          # no-op unless the MCP server is enabled

        self._refresh_character_list()
        self._load_cached_dockables()
        if self.token:
            self._fetch_contacts()
            self._fetch_starbases()
            self._fetch_location()
            self._sync_location_tracking()
        self._fetch_incursions()
        self.refresh_intel()
        self._start_intel_timer()
        self._fetch_sovereignty()
        self._fetch_wormholes()
        self._resolve_docking_rights()
        from .. import update as _upd
        if _upd.auto_check_enabled() and _upd.is_frozen():
            self._check_updates()
        self._load_universe()

    def _fetch_wormholes(self, force: bool = False):
        """Pull the scouted Thera/Turnur connections and install them.

        Cached on disk, because the route panel asks for these on every
        replan and EVE-Scout is volunteer-run. ``force`` skips the cache for
        the explicit refresh action -- connections expire in hours, so before
        committing a freighter you want the current list, not a 15-minute-old
        one.
        """
        from ..esi import evescout

        cached = {} if force else evescout.load()
        if cached.get("rows") and not cached.get("stale"):
            self._on_wormholes(cached)
            return
        w = Worker(evescout.refresh)
        w.finished_ok.connect(self._on_wormholes)
        w.failed.connect(lambda m: self.statusBar().showMessage(
            f"EVE-Scout: {m}", 8000))
        self._run(w)
        self._fetch_wanderer(force)

    def _fetch_wanderer(self, force: bool = False):
        """Pull the user's own Wanderer map, if they configured one."""
        from ..esi import wanderer

        if not config.wanderer_configured():
            return
        cached = {} if force else wanderer.load()
        if cached.get("connections"):
            self._on_wanderer(cached)
            return
        w = Worker(wanderer.refresh)
        w.finished_ok.connect(self._on_wanderer)
        w.failed.connect(lambda m: self.statusBar().showMessage(
            f"Wanderer: {m}", 8000))
        self._run(w)

    def _on_wanderer(self, data):
        from ..esi import wanderer

        self._wanderer_data = data or {}
        if self.universe is None:
            return          # re-applied by _on_universe once the map exists
        self._install_wormholes()
        if self._wanderer_data:
            self.statusBar().showMessage(wanderer.describe(self._wanderer_data),
                                         6000)

    def _edit_wanderer(self):
        from .dialogs import WandererDialog
        dlg = WandererDialog(self, config.get_wanderer_url(),
                             config.get_wanderer_map(),
                             config.get_wanderer_token())
        if not dlg.exec():
            return
        config.set_wanderer(*dlg.values())
        # Settings changed, so any cached map is for the wrong instance.
        self._wanderer_data = {}
        self._fetch_wanderer(force=True)

    def _on_wormholes(self, data):
        from ..esi import evescout

        self._hole_data = data or {}
        if self.universe is None:
            return          # installed by _on_universe once the map is loaded
        n, conns = self._install_wormholes()
        if n:
            self.statusBar().showMessage(
                f"EVE-Scout: {len(conns)} connections to k-space "
                f"({n} routes).", 5000)

    def _install_wormholes(self):
        """Merge every wormhole source into one edge set and install it.

        set_wormholes replaces wholesale, so both sources have to be combined
        here rather than each installing its own. Where the two describe the
        same pair the roomier edge wins, since that is the one that decides
        whether the hull fits.
        """
        from ..esi import evescout, wanderer

        rows = (self._hole_data or {}).get("rows") or []
        conns = evescout.connections(rows, self.universe.systems)
        turnur = self.universe.by_name("Turnur")
        edges = dict(evescout.graph(conns, turnur.id if turnur else None))

        for key, info in wanderer.edges(getattr(self, "_wanderer_data", {}) or {},
                                        self.universe.systems).items():
            old = edges.get(key)
            if old is None or info["max_t"] > old["max_t"]:
                edges[key] = info

        n = self.universe.set_wormholes(edges)
        # Which hubs each system connects to, for the map tooltip.
        hub_of: dict[int, set] = {}
        for c in conns:
            hub_of.setdefault(c["system_id"], set()).add(c["hub"])
        for (a, b), info in edges.items():
            if info.get("via") == "Wanderer":
                hub_of.setdefault(a, set()).add("Wanderer")
                hub_of.setdefault(b, set()).add("Wanderer")
        if self.map_view:
            self.map_view.refresh_wormholes(hub_of)
        self._sync_hole_status()
        return n, conns

    def _sync_hole_status(self):
        """Tell the route panel what the connections are worth to this hull."""
        from ..esi import evescout

        if not (self._built and self.universe):
            return
        info = self.universe.hole_info
        hull = self.ship.current_ship().hull_class
        passable = sum(1 for i in info.values()
                       if evescout.fits(hull, i))
        self.route.set_hole_status(len(info), passable, hull,
                                   bool(self._hole_data.get("stale")))

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
            # Only rebuild if the layer is actually on; baking costs a second
            # of worker time and an off layer should cost nothing.
            if self.act_layers["sov"].isChecked():
                self.refresh_sov_territory()

    def sov_label(self, system_id: int):
        """Short owner label for map hover, e.g. 'Goonswarm Federation'."""
        info = self.sov_of(system_id)
        return info[0] if info else None

    # -- about ---------------------------------------------------------------
    def _open_about(self):
        from .dialogs import AboutDialog
        AboutDialog(self).exec()

    # -- updates ------------------------------------------------------------
    def _check_updates(self, explicit: bool = False):
        from .. import __version__, update
        w = Worker(update.check, current=__version__)

        def done(info):
            if not info:
                if explicit:
                    QMessageBox.information(
                        self, "Up to date",
                        f"Eve-Strait {__version__} is the latest release.")
                return
            self._offer_update(info)

        w.finished_ok.connect(done)
        w.failed.connect(lambda m: QMessageBox.warning(self, "Update", m)
                         if explicit else None)
        self._run(w)

    def _offer_update(self, info: dict):
        from .. import __version__, update
        from .dialogs import UpdateDialog
        can_install = (update.is_frozen() and bool(info.get("zip_url"))
                       and update.can_write_install_dir())
        dlg = UpdateDialog(self, info, __version__, can_install)
        if not can_install and update.is_frozen():
            dlg.status.setText(
                "This install folder is not writable, so it cannot update "
                "itself. Download the new version from the release page.")

        def start():
            dlg.set_busy("Downloading...")
            dw = Worker(update.download, info["zip_url"],
                        dest_dir=update.staging_dir())
            dw.progress.connect(dlg.set_busy)

            def ready(zip_path):
                try:
                    update.apply_and_restart(zip_path, dlg.set_busy)
                except Exception as exc:      # noqa: BLE001 - show, don't die
                    dlg.status.setText(f"Update failed: {exc}")

            dw.finished_ok.connect(ready)
            dw.failed.connect(lambda m: dlg.status.setText(f"Download failed: {m}"))
            self._run(dw)

        dlg.btn_install.clicked.connect(start)
        dlg.exec()

    # -- characters ---------------------------------------------------------
    def _refresh_character_list(self):
        chars = sorted((cid, t.character_name) for cid, t in self.tokens.items())
        active = self.token.character_id if self.token else None
        self.character.set_characters(chars, active)
        self.character.set_login(self.token.character_name if self.token else None)

    def _switch_character(self, character_id: int):
        """Make another linked character active and reload everything of theirs."""
        token = self.tokens.get(character_id)
        if not token or (self.token and token.character_id == self.token.character_id):
            return
        auth.set_active(character_id)
        self.token = token
        self.esi = EsiClient(token, config.get_client_id())
        # Everything below is per-character; drop the previous character's data.
        self.dockables = []
        self.standings = {}
        self.starbase_systems = {}
        self.location_system_id = None
        self.my_corp_id = self.my_alliance_id = None
        self._structs_fetched.clear()
        self.character.set_dockables([])
        self.character.set_location("")
        self._refresh_character_list()
        self._load_cached_dockables()
        self._fetch_contacts()
        self._fetch_starbases()
        self._fetch_location()
        self._fetch_ship_type()
        self._sync_location_tracking()
        self._render_character()
        self.route.refresh()
        self.statusBar().showMessage(f"Switched to {token.character_name}.", 4000)

    def _unlink_character(self, character_id: int):
        token = self.tokens.get(character_id)
        if not token:
            return
        if QMessageBox.question(
                self, "Unlink character",
                f"Remove {token.character_name} from Eve-Strait?\n"
                "Their cached asset list is kept; you can re-add them later."
        ) != QMessageBox.StandardButton.Yes:
            return
        auth.remove(character_id)
        self.tokens.pop(character_id, None)
        remaining = auth.load_saved()
        if remaining:
            self.token = remaining
            self.esi = EsiClient(remaining, config.get_client_id())
            self._switch_character(remaining.character_id)
        else:
            self.token = None
            self.esi = None
            self.dockables = []
            self.character.set_dockables([])
        self._sync_location_tracking()
        self._refresh_character_list()
        self._render_character()

    # -- current location ---------------------------------------------------
    def _fetch_location(self):
        """Where the active character is (scope already granted at login)."""
        if not self.token:
            return
        if not self.esi:
            self.esi = EsiClient(self.token, config.get_client_id())
        w = Worker(lambda: self.esi.location(priority="background"))
        w.finished_ok.connect(self._on_location)
        w.failed.connect(lambda m: self.character.set_location(""))
        self._run(w, "Checking character location…")

    def _on_location(self, data):
        # Re-arm from the current budget: the first poll is what teaches
        # the governor this group's real limit, so the interval can only
        # be correct from the second poll onwards.
        timer = getattr(self, "_location_timer", None)
        if timer is not None and timer.isActive():
            timer.setInterval(self._location_poll_ms())
        sid = (data or {}).get("solar_system_id")
        self.location_system_id = sid
        if not sid or not self.universe:
            self.character.set_location("")
            return
        system = self.universe.systems.get(sid)
        if not system:
            self.character.set_location("")
            return
        region = self.universe.region_names.get(system.region_id, "")
        self.character.set_location(
            f"Currently in <b>{system.name}</b> ({system.security:.1f})"
            + (f", {region}" if region else ""))
        if self.map_view:
            self.map_view.set_current_location(sid)
            # Only in Follow Me: auto-waypoint's own 30s-floor polling is
            # too infrequent to justify yanking the view around every time
            # it lands, but Follow Me exists specifically to keep the "you
            # are here" marker current, so a jump that carries it off screen
            # should bring it back.
            if self.character.chk_follow.isChecked():
                self.map_view.ensure_location_visible(sid)
        self._maybe_fire_auto_waypoint(sid)

    def _fetch_ship_type(self):
        """Which hull the active character is in right now, to preselect
        the Ship dropdown on login/switch. Fired alongside _fetch_location()
        so both the location and hull the UI shows are current immediately
        rather than waiting on whatever the previous character had set.
        """
        if not self.token:
            return
        if not self.esi:
            self.esi = EsiClient(self.token, config.get_client_id())
        w = Worker(lambda: self.esi.ship(priority="background"))
        w.finished_ok.connect(self._on_ship_probe)
        w.failed.connect(lambda m: None)  # no ship scope, offline, etc: leave the dropdown as-is
        self._run(w, "Checking current ship…")

    def _on_ship_probe(self, data):
        # Never override a ship the user already picked to plan an actual
        # route -- only apply the probe while the Ship panel is still just
        # sitting at its defaults/last selection with nothing built on it.
        if self.route.has_route():
            return
        type_id = (data or {}).get("ship_type_id")
        if not type_id or not self.universe:
            return
        hull = self.universe.ship_hull_type_ids.get(type_id)
        if hull and self.ship.set_current_ship(hull):
            self.statusBar().showMessage(f"Ship set to {hull} (currently flying).", 4000)

    def _use_location_as_origin(self):
        """Put the character's current system at the head of the route."""
        if self.location_system_id is None:
            self.statusBar().showMessage(
                "Current location unknown. Log in and make sure the character is online.",
                6000)
            self._fetch_location()
            return
        self.route.set_origin(self.location_system_id)

    # -- system activity (kills, pods, traffic) -----------------------------
    def _fetch_activity(self, force: bool = False, priority: str = "background"):
        from ..esi import client
        w = Worker(lambda: client.system_activity(force=force, priority=priority))
        w.finished_ok.connect(self._on_activity)
        w.failed.connect(lambda m: None)
        if force:
            self._track_force_worker(w)
        self._run(w, "Loading kill and traffic activity…")

    # -- heat map data refresh ---------------------------------------------
    def _track_force_worker(self, worker):
        """Count a forced-refresh worker so the menu item can stay disabled.

        refresh_intel() fans out to three workers, so a boolean would clear on
        the first one home and let a second force run start while two are
        still in flight.
        """
        self._force_intel_outstanding += 1
        worker.finished.connect(self._force_worker_done)
        self._sync_force_action()

    def _force_worker_done(self):
        self._force_intel_outstanding = max(0, self._force_intel_outstanding - 1)
        self._sync_force_action()

    def _sync_force_action(self):
        act = getattr(self, "act_force_heat", None)
        if act is not None:
            act.setEnabled(self._force_intel_outstanding == 0)

    def _heatmap_data_age(self) -> str:
        """When the kill/traffic layer was last read, and when it renews."""
        import time
        from ..esi.transport import get_transport
        st = get_transport().cache_status("/universe/system_kills/")
        if not st:
            return ""
        seen = time.strftime("%H:%M", time.localtime(st.fetched_at))
        due = time.strftime("%H:%M", time.localtime(st.expires_at))
        return f" Data as of {seen}, next update {due}."

    def _refresh_heatmap(self, force: bool = False):
        """Menu-driven intel refresh.

        Interactive priority, not background: the governor may decline
        background work when the budget is low, and a menu item the user just
        clicked should not be the thing that silently does nothing.
        """
        if force:
            if self._force_intel_outstanding:
                return                      # already running; item is greyed
            from ..esi.transport import get_transport
            import time
            st = get_transport().cache_status("/universe/system_kills/")
            still_fresh = bool(st and st.expires_at > time.time())
            if still_fresh and QMessageBox.question(
                    self, "Force re-download",
                    "EVE has not published new heat map data yet"
                    f"{self._heatmap_data_age()}" + "\n\n"
                    "Re-downloading now returns the same numbers and spends "
                    "part of your rate-limit budget. Continue?"
            ) != QMessageBox.StandardButton.Yes:
                return
        self.refresh_intel(force=force, priority="interactive")
        self.statusBar().showMessage(
            ("Forcing heat map re-download." if force
             else "Refreshing heat map data.") + self._heatmap_data_age(), 8000)

    def refresh_intel(self, force: bool = False, priority: str = "background"):
        """Re-poll every activity source: kills, traffic, ADM, industry."""
        self._fetch_activity(force=force, priority=priority)
        self._fetch_defense(force=force, priority=priority)

    # Live location is only worth polling ESI for while something actually
    # wants it -- right now that is only the auto-waypoint feature.
    #
    # The interval is derived from the rate-limit headers rather than fixed:
    # /characters/{id}/location/ sits in the character-location group, and at
    # the old fixed 15s this poll alone burned 120 tokens per 15-minute
    # window -- around 80% of a 150/15m bucket -- so a cyno scan touching the
    # same bucket would tip it into a 429. The governor gives background work
    # half the group's budget; the 30s floor keeps us above the endpoint's own
    # server-side cache, below which polling learns nothing new anyway.
    LOCATION_POLL_FLOOR_MS = 30_000

    # "Follow Me" wants a livelier marker than auto-waypoint alone needs, so
    # it's allowed a lower floor -- but the floor is still a floor, not a
    # fixed interval: ESI's own docs say /characters/{id}/location/ is
    # server-cached for 5 seconds and that circumventing that cache can get
    # an application banned, so 5s is the honest bottom, and the governor
    # (RateLimitGovernor.poll_interval) is still the one deciding the actual
    # number above that floor based on the real remaining budget. Never
    # replace this with a flat interval -- see AGENTS.md's rate-limiting
    # section for why.
    FOLLOW_ME_POLL_FLOOR_MS = 5_000

    def _location_poll_ms(self) -> int:
        if not self.token:
            return self.LOCATION_POLL_FLOOR_MS
        floor_ms = (self.FOLLOW_ME_POLL_FLOOR_MS
                    if self.character.chk_follow.isChecked()
                    else self.LOCATION_POLL_FLOOR_MS)
        from ..esi.transport import get_transport
        seconds = get_transport().governor.poll_interval(
            f"/characters/{self.token.character_id}/location/",
            self.token.character_id,
            floor=floor_ms / 1000)
        return int(seconds * 1000)

    def _sync_location_tracking(self):
        """Poll location on a timer while something wants a live position:
        the auto-waypoint feature (armed) or "Follow Me" (checked), and a
        character to poll for either way."""
        from PySide6.QtCore import QTimer
        want = (bool(self.token)
                and (self.route.chk_auto_waypoint.isChecked()
                     or self.character.chk_follow.isChecked()))
        if want:
            if getattr(self, "_location_timer", None) is None:
                self._location_timer = QTimer(self)
                self._location_timer.timeout.connect(self._fetch_location)
            if not self._location_timer.isActive():
                self._location_timer.start(self._location_poll_ms())
                self._fetch_location()  # don't wait a full interval to arm
            else:
                # Follow Me toggling on/off changes the floor mid-flight;
                # re-arm so the new interval takes effect without waiting
                # out whatever was left of the old one.
                self._location_timer.setInterval(self._location_poll_ms())
        elif getattr(self, "_location_timer", None) is not None:
            self._location_timer.stop()

    def _on_follow_me_toggled(self, on: bool):
        config.set_follow_me(on)
        self._sync_location_tracking()

    def _maybe_fire_auto_waypoint(self, current_system_id: int):
        """Called from _on_location every time it updates. Fires the in-game
        waypoint at most once per distinct route -- the key includes every
        waypoint, so any route edit re-arms it rather than leaving a stale
        firing decision in place."""
        if not self.route.chk_auto_waypoint.isChecked():
            return
        target = self.route.auto_waypoint_target()
        if target is None:
            return
        trigger, dest, dock_location_id = target
        if current_system_id != trigger.id:
            return
        key = (tuple(s.id for s in self.route.systems()), trigger.id)
        if getattr(self, "_auto_waypoint_fired_for", None) == key:
            return
        self._auto_waypoint_fired_for = key
        if dock_location_id:
            self.set_ingame_waypoint(dock_location_id, clear_other_waypoints=True,
                                     label=dest.name, silent=True)
        else:
            self.set_ingame_waypoint(dest.id, clear_other_waypoints=True, silent=True)
        self.statusBar().showMessage(
            f"Reached {trigger.name} - in-game destination set to {dest.name}.", 8000)

    def _start_intel_timer(self):
        """Re-poll on the configured interval. 0 minutes means never.

        Single-shot and re-armed on completion rather than a repeating timer:
        CCP asks that periodic jobs schedule from the end of the last run,
        with some spread, so that every copy of an app does not stampede the
        same endpoints at the same moment.
        """
        from PySide6.QtCore import QTimer
        if getattr(self, "_intel_timer", None) is None:
            self._intel_timer = QTimer(self)
            self._intel_timer.setSingleShot(True)
            self._intel_timer.timeout.connect(self._run_intel_refresh)
        self._arm_intel_timer()

    def _arm_intel_timer(self):
        import random
        minutes = config.get_intel_refresh_minutes()
        if minutes <= 0:
            self._intel_timer.stop()
            return
        base = minutes * 60 * 1000
        self._intel_timer.start(base + random.randint(0, 60_000))

    def _run_intel_refresh(self):
        # try/finally: a failed refresh must still re-arm, or one transient
        # network error silently stops intel updating for the whole session.
        try:
            self.refresh_intel()
        finally:
            self._arm_intel_timer()

    def _edit_intel_settings(self):
        from ..esi import intel_store
        from .dialogs import IntelSettingsDialog
        dlg = IntelSettingsDialog(self, config.get_intel_refresh_minutes())
        dlg.set_history_days(config.get_intel_history_days())
        dlg.set_stats(intel_store.stats())

        def purge():
            if QMessageBox.question(
                    self, "Delete history",
                    "Delete all stored intel history? This cannot be undone."
            ) == QMessageBox.StandardButton.Yes:
                intel_store.purge()
                dlg.set_stats(intel_store.stats())

        dlg.btn_purge.clicked.connect(purge)
        if not dlg.exec():
            return
        config.set_intel_refresh_minutes(dlg.minutes())
        config.set_intel_history_days(dlg.history_days())
        self._start_intel_timer()
        if dlg.refresh_now():
            self.refresh_intel()
        mins = config.get_intel_refresh_minutes()
        self.statusBar().showMessage(
            "Intel refresh disabled." if mins <= 0
            else f"Intel refreshes every {mins} min.", 5000)

    def _fetch_defense(self, force: bool = False, priority: str = "background"):
        """ADM and industry indices: the 'is anyone actually here' signals."""
        from ..esi import client
        def arrived(attr, key):
            def slot(d):
                setattr(self, attr, d or {})
                if self._heat_key == key:
                    self._set_heat_layer(key)
            return slot

        w = Worker(lambda: client.sovereignty_defense(force=force,
                                                      priority=priority))
        w.finished_ok.connect(arrived("sov_defense", "adm"))
        w.failed.connect(lambda m: None)
        if force:
            self._track_force_worker(w)
        self._run(w, "Loading sovereignty defense…")
        w2 = Worker(lambda: client.industry_indices(force=force,
                                                    priority=priority))
        w2.finished_ok.connect(arrived("industry_index", "industry"))
        w2.failed.connect(lambda m: None)
        if force:
            self._track_force_worker(w2)
        self._run(w2, "Loading industry indices…")

    def system_intel(self, system_id: int) -> dict:
        """Everything we know about activity in one system.

        ESI has no per-system player count, so "how busy is this" is assembled
        from proxies: gate traffic, NPC kills (ratting), ADM (mining, ratting
        and industry over time) and industry indices.
        """
        hist = self.activity_totals
        k = self.kill_activity.get(system_id, {})
        defense = self.sov_defense.get(system_id, {})
        return {
            "jumps_1h": self.jump_activity.get(system_id, 0),
            "jumps_24h": hist.get("jumps", {}).get(system_id, 0),
            "ship_kills_1h": k.get("ship", 0),
            "pod_kills_1h": k.get("pod", 0),
            "npc_kills_1h": k.get("npc", 0),
            "kills_24h": hist.get("kills", {}).get(system_id, {}),
            "history_hours": hist.get("hours", 0),
            "adm": defense.get("adm"),
            "vuln_start": defense.get("vuln_start", ""),
            "vuln_end": defense.get("vuln_end", ""),
            "industry": self.industry_index.get(system_id, {}),
        }

    def _on_activity(self, result):
        result = result or {}
        self.kill_activity = result.get("kills", {})
        self.jump_activity = result.get("jumps", {})
        # Repaint if the active layer is drawn from what just arrived. Only
        # jumps_24h used to be refreshed here, so a kills or 1h-traffic layer
        # chosen before the data landed stayed blank until re-picked by hand.
        if self._heat_key in ("jumps_1h", "ship_kills", "pod_kills",
                              "npc_kills"):
            self._set_heat_layer(self._heat_key)
        # ESI only reports the last hour, so build 24h totals ourselves.
        # Both writes go to a worker: the rolling window is small, but the
        # long-term SQLite store can be thousands of rows and must not run on
        # the UI thread.
        defense = dict(self.sov_defense)

        def persist():
            from ..esi import activity_history, intel_store
            activity_history.record(result)
            intel_store.record(result, defense)
            return activity_history.totals()

        def took(totals):
            self.activity_totals = totals
            if self._heat_key in ("jumps_24h", "ship_kills_24h",
                                  "pod_kills_24h", "npc_kills_24h"):
                self._set_heat_layer(self._heat_key)

        w = Worker(persist)
        w.finished_ok.connect(took)
        w.failed.connect(lambda m: None)
        self._run(w)
        if self.kill_activity:
            busy = sum(1 for v in self.kill_activity.values() if v["ship"])
            self.statusBar().showMessage(
                f"Kill activity loaded: {busy} systems with recent ship kills.", 5000)
            if self.map_view:
                self.map_view.set_kill_activity(self.kill_activity)
            self.route.refresh()
        if self._heat_key in ("jumps_1h", "ship_kills", "pod_kills", "npc_kills"):
            self._set_heat_layer(self._heat_key)

    def kills_in(self, system_id: int) -> dict:
        return self.kill_activity.get(system_id, {"ship": 0, "pod": 0, "npc": 0})

    def jumps_in(self, system_id: int) -> int:
        return self.jump_activity.get(system_id, 0)

    def danger_predicate(self):
        """Systems with recent player kills, for the router's danger bias."""
        kills = self.kill_activity
        if not kills or not self.route.avoid_kills():
            return None
        return lambda sid: bool(kills.get(sid, {}).get("ship"))

    # -- cyno jammers -------------------------------------------------------
    def jammed_systems(self) -> set:
        """Systems with a known Tenebrex Cyno Jammer.

        A jammer stops cynos being lit, so nothing can jump *into* the system.
        Gates still work, which is exactly what the router needs to model.
        """
        from ..data import docking
        out = set()
        for d in self.dockables:
            if docking.is_cyno_jammer(getattr(d, "type_id", 0)):
                out.add(d.solar_system_id)
        return out

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

    def starbases_in(self, system_id: int) -> int:
        return self.starbase_systems.get(system_id, 0)

    def _fetch_starbases(self):
        """Corp POS towers (needs the Director role); silent if unavailable."""
        if not self.token:
            return
        if not self.esi:
            self.esi = EsiClient(self.token, config.get_client_id())
        w = Worker(self.esi.starbases)

        def done(result):
            result = result or {}
            self.starbase_systems = result.get("systems", {})
            if self.starbase_systems:
                self.statusBar().showMessage(
                    f"Found corp POS in {len(self.starbase_systems)} system(s).", 5000)
                self.route.refresh()
            elif result.get("errors"):
                self.statusBar().showMessage(result["errors"][0], 8000)

        w.finished_ok.connect(done)
        w.failed.connect(lambda m: None)
        self._run(w)

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

    def route_image(self):
        """A framed picture of the current route, captioned with its summary."""
        if not self.map_view or not self.route.waypoints:
            return None
        names = [w.system.name for w in self.route.waypoints]
        ends = f"{names[0]} → {names[-1]}" if len(names) > 1 else names[0]
        via = f" ({len(names)} waypoints)" if len(names) > 2 else ""
        summary = self.route.totals.text().split(" · ⚠")[0]
        caption = f"{ends}{via} · {self.ship_name()} · {summary}"
        return self.map_view.route_image([w.system.id for w in self.route.waypoints],
                                         caption)

    # -- Dotlan interchange -------------------------------------------------
    def ship_name(self) -> str:
        ship = self.ship.current_ship()
        return getattr(ship, "name", "") if ship else ""

    def jump_skills(self):
        """JDC, JFC, JF - the three Dotlan encodes, in the order it writes them.

        Jump Drive Operation is deliberately not here: Dotlan's jump options do
        not carry it, because it moves capacitor rather than range or fuel.
        """
        return (self.ship.sp_jdc.value(), self.ship.sp_jfc.value(),
                self.ship.sp_jf.value())

    def _on_dotlan_imported(self, route):
        """Load a pasted Dotlan link: ship, skills and waypoints."""
        if self.universe is None:
            QMessageBox.information(self, "Dotlan link",
                                    "The map is still loading. Try again in a "
                                    "moment.")
            return
        notes = []

        if route.ship:
            idx = self.ship.ship_combo.findText(route.ship,
                                                Qt.MatchFlag.MatchStartsWith)
            if idx >= 0:
                self.ship.ship_combo.setCurrentIndex(idx)
            else:
                # Dotlan knows hulls this app does not model, so say which
                # rather than silently planning with the wrong ship.
                notes.append(f"Ship {route.ship!r} is not one this app knows; "
                             f"kept {self.ship_name()!r}.")

        for value, spin in ((route.jdc, self.ship.sp_jdc),
                            (route.jfc, self.ship.sp_jfc),
                            (route.jf, self.ship.sp_jf)):
            if value is not None:
                spin.setValue(value)

        found, missing = [], []
        for name in route.systems:
            s = self.universe.by_name(name)
            (found.append(s) if s else missing.append(name))
        if found:
            self.route.clear_waypoints()
            for s in found:
                self.route.add_system(s.id)
        if missing:
            notes.append("Unknown system(s): " + ", ".join(missing[:6]))

        msg = f"Loaded {len(found)} waypoint(s) from the Dotlan link."
        if notes:
            msg += "\n\n" + "\n".join(notes)
        QMessageBox.information(self, "Dotlan link", msg)

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
        # Eight separate modals used to live here, one per menu item, so
        # changing two related things meant two trips through the menu and
        # nothing was findable unless you already knew it was there.
        for text, slot in (
            ("Settings...", self._open_settings),
            ("Reload map data", self._reload_map),
            ("Log out", self._logout),
            ("Quit", self.close),
        ):
            a = QAction(text, self)
            a.triggered.connect(slot)
            m.addAction(a)

        help_menu = self.menuBar().addMenu("&Help")
        act_upd = QAction("Check for updates...", self)
        act_upd.triggered.connect(lambda: self._check_updates(explicit=True))
        help_menu.addAction(act_upd)
        from .. import update as _update
        self.act_auto_update = QAction("Check for updates at startup", self,
                                       checkable=True)
        self.act_auto_update.setChecked(_update.auto_check_enabled())
        self.act_auto_update.toggled.connect(_update.set_auto_check)
        help_menu.addAction(self.act_auto_update)
        help_menu.addSeparator()
        act_about = QAction("About Eve-Strait", self)
        act_about.triggered.connect(self._open_about)
        help_menu.addAction(act_about)

        view = self.menuBar().addMenu("&View")
        act_reset = QAction("Reset all panels", self)
        act_reset.setToolTip("Re-dock every panel and restore the default sizes.")
        act_reset.triggered.connect(self.reset_panels)
        view.addAction(act_reset)
        view.addSeparator()

        # -- map layers, each independently switchable -----------------------
        layers = view.addMenu("Map layers")
        self.act_layers = {}
        for key, text, default, tip in self.MAP_LAYERS:
            a = QAction(text, self, checkable=True)
            a.setChecked(default)
            a.setToolTip(tip)
            a.toggled.connect(lambda on, k=key: self._toggle_layer(k, on))
            layers.addAction(a)
            self.act_layers[key] = a
        # Kept as its own attribute: older saved settings and _on_universe
        # both reference it by name.
        self.act_gate_links = self.act_layers["gates"]

        layers.addSeparator()
        act_all_off = QAction("Turn all layers off", self)
        act_all_off.triggered.connect(lambda: self._set_all_layers(False))
        layers.addAction(act_all_off)
        act_all_on = QAction("Turn all layers on", self)
        act_all_on.triggered.connect(lambda: self._set_all_layers(True))
        layers.addAction(act_all_on)

        # -- heat map: one metric at a time ----------------------------------
        heat = view.addMenu("Heat map")
        self._heat_group = QActionGroup(self)
        self._heat_group.setExclusive(True)
        self.act_heat = {}
        for key, text, tip in self.HEAT_LAYERS:
            a = QAction(text, self, checkable=True)
            a.setToolTip(tip)
            a.setChecked(key == "none")
            self._heat_group.addAction(a)
            heat.addAction(a)
            self.act_heat[key] = a
            a.triggered.connect(lambda _c, k=key: self._set_heat_layer(k))
            if key == "none":
                heat.addSeparator()
        self._heat_key = "none"

        heat.addSeparator()
        act_refresh = QAction("Refresh Heat Map Data", self)
        act_refresh.setToolTip(
            "Re-poll kills, traffic, ADM and industry now instead of waiting "
            "for the timer. Respects EVE's own cache, so it is nearly free.")
        act_refresh.triggered.connect(lambda: self._refresh_heatmap(False))
        heat.addAction(act_refresh)

        self.act_force_heat = QAction("Force re-download (ignores cache)", self)
        self.act_force_heat.setToolTip(
            "Re-download even if EVE's copy has not changed yet. Spends part "
            "of your rate-limit budget; disabled while one is already running.")
        self.act_force_heat.triggered.connect(lambda: self._refresh_heatmap(True))
        heat.addAction(self.act_force_heat)

        view.addSeparator()
        self.act_scan_cyno = QAction("Scan for cyno activity...", self)
        self.act_scan_cyno.setToolTip(
            "Walk killmails region by region looking for ships that died with "
            "a cyno fitted. The only cyno signal that exists outside the game.")
        self.act_scan_cyno.triggered.connect(self._scan_cynos)
        view.addAction(self.act_scan_cyno)

    # Map layers: (settings key, menu text, on by default, tooltip).
    MAP_LAYERS = (
        ("gates", "Stargate links", True, "The gate mesh between systems."),
        ("bridges", "Ansiblex bridges", True, "Your configured jump gates."),
        ("regions", "Region names", True, "Region labels behind the map."),
        ("kills", "Recent kill rings", False,
         "Ring systems with player kills in the last hour. Nearly 3000 "
         "systems qualify at any time, so this is busy by design."),
        ("avoid", "Avoided systems", True, "Red X on systems you never route through."),
        ("location", "Current location", True, "Cyan ring on your active character."),
        ("notes", "System notes", True,
         "Amber tag on systems you have written a note about."),
        ("holes", "EVE-Scout wormholes", False,
         "Ring systems with a scouted Thera or Turnur connection. Volunteer "
         "data that expires in hours, so off by default."),
        ("sov", "Sovereignty territory", False,
         "Fill null-sec space by who holds it. Off by default: it is the "
         "heaviest layer to draw."),
        ("cyno_alts", "My cyno alts", True,
         "Ring and name the systems where one of your own characters is "
         "sitting in a cyno-fitted ship. Populated by Scan my characters, "
         "in the character panel."),
        ("heat", "Heat map", True, "The metric shading chosen below."),
    )

    # Heat metrics: (key, menu text, tooltip). Every one of these comes from
    # an hourly ESI snapshot, so "recent" means the last full hour.
    HEAT_LAYERS = (
        ("none", "None", "No shading."),
        ("jumps_1h", "Gate traffic, last hour",
         "Ship jumps through the system's gates. ESI publishes no per-system "
         "player count, so this is the closest thing to 'how busy is it'."),
        ("jumps_24h", "Gate traffic, last 24h",
         "Accumulated locally while the app runs; partial until it has been "
         "open for a day."),
        ("npc_kills", "Ratting (NPC kills, 1h)",
         "NPC kills. High in null means someone is farming anomalies."),
        ("npc_kills_24h", "Ratting (NPC kills, 24h)",
         "NPC kills, accumulated locally the same way gate traffic's 24h "
         "figure is -- ESI only ever reports the last hour, so this is only "
         "as complete as the app has been open for. Partial until a full "
         "24h has passed."),
        ("ship_kills", "Player ship kills, last hour", "Where people are dying."),
        ("ship_kills_24h", "Player ship kills, last 24h",
         "Accumulated locally; partial until it has been open for a day."),
        ("pod_kills", "Pod kills, last hour", "Podded, so a fight went badly."),
        ("pod_kills_24h", "Pod kills, last 24h",
         "Accumulated locally; partial until it has been open for a day."),
        ("adm", "Sovereignty ADM",
         "Activity Defense Multiplier, 1 to 6. Raised by ratting, mining and "
         "industry in the system, so a high ADM means a used system."),
        ("industry", "Industry cost index",
         "Manufacturing cost index. Rises with local production."),
        ("cyno", "Cyno activity (killmails)",
         "Ships destroyed with a cynosural field generator fitted. ESI "
         "publishes nothing about cynos, so this is built from killmails: a "
         "floor on cyno traffic, never a count. Run View -> Scan for cyno "
         "activity to populate it."),
    )

    def _toggle_layer(self, key: str, visible: bool):
        if self.map_view:
            self.map_view.set_overlay_visible(key, visible)
            # Territory is baked lazily the first time it is switched on.
            if key == "sov" and visible and not self.map_view._sov_items:
                self.refresh_sov_territory()
        self._save_settings()

    def _set_all_layers(self, on: bool):
        for key, a in self.act_layers.items():
            a.blockSignals(True)
            a.setChecked(on)
            a.blockSignals(False)
            if self.map_view:
                self.map_view.set_overlay_visible(key, on)
        self._save_settings()

    # Short noun for the hover line under a system name ("Jita / 1,204
    # jumps (1h)"). Separate from the HEAT_LAYERS menu text because that text
    # is a sentence fragment for a menu ("Gate traffic, last hour") and far
    # too long to hang off a system name on the map. Kept as its own table so
    # the menu tuples stay a 3-tuple and this is trivially editable.
    HEAT_UNITS = {
        "jumps_1h": "jumps (1h)",
        "jumps_24h": "jumps (24h)",
        "npc_kills": "NPC kills (1h)",
        "npc_kills_24h": "NPC kills (24h)",
        "ship_kills": "ship kills (1h)",
        "ship_kills_24h": "ship kills (24h)",
        "pod_kills": "pod kills (1h)",
        "pod_kills_24h": "pod kills (24h)",
        "adm": "ADM",
        "industry": "cost index",
        "cyno": "cyno-fitted losses",
    }

    def _set_heat_layer(self, key: str):
        """Recompute the heat layer from whatever intel we already hold."""
        self._heat_key = key
        self._save_settings()
        if not self.map_view:
            return
        if key == "none":
            self.map_view.set_heat(None)
            return

        kills = self.kill_activity or {}
        totals = self.activity_totals or {}
        label = dict((k, t) for k, t, _ in self.HEAT_LAYERS).get(key, key)
        unit = self.HEAT_UNITS.get(key, "")
        if key == "jumps_1h":
            values = dict(self.jump_activity or {})
        elif key == "jumps_24h":
            values = dict(totals.get("jumps") or {})
            hours = totals.get("hours", 0)
            if hours < 24:
                label = f"{label} ({hours}h so far)"
                unit = f"jumps ({hours}h so far)"
        elif key in ("ship_kills", "pod_kills", "npc_kills"):
            field = key.split("_")[0]
            values = {sid: c.get(field, 0) for sid, c in kills.items()}
        elif key in ("ship_kills_24h", "pod_kills_24h", "npc_kills_24h"):
            field = key.split("_")[0]
            values = {sid: c.get(field, 0)
                     for sid, c in (totals.get("kills") or {}).items()}
            hours = totals.get("hours", 0)
            if hours < 24:
                label = f"{label} ({hours}h so far)"
                unit = unit.replace("(24h)", f"({hours}h so far)")
        elif key == "adm":
            values = {sid: d.get("adm") or 0
                      for sid, d in (self.sov_defense or {}).items()}
        elif key == "industry":
            values = {sid: (d.get("manufacturing") or 0) * 100
                      for sid, d in (self.industry_index or {}).items()}
        elif key == "cyno":
            values = dict(self.cyno_activity or {})
            hours = self._cyno_sweep.get("hours", 24)
            label = f"Cyno-fitted losses, {hours}h"
            unit = f"cyno-fitted losses ({hours}h)"
        else:
            values = {}

        if not values:
            if key == "cyno":
                self.statusBar().showMessage(
                    "No cyno sweep data yet. Use View -> Scan for cyno "
                    "activity to build it.", 8000)
            else:
                self.statusBar().showMessage(
                    f"No {label.lower()} data yet. It arrives with the next "
                    "intel refresh (Settings -> Intel).", 8000)
        self.map_view.set_heat(values, label, unit)

    # -- AI assistant -------------------------------------------------------
    def _edit_ai_settings(self):
        from .ai_dialog import AiSettingsDialog
        dlg = AiSettingsDialog(self)
        if not dlg.exec():
            return
        dlg.save()
        self._sync_chat_panel()
        self._sync_bridge()

    def _sync_chat_panel(self):
        """Create or tear down the chat dock to match the configuration.

        Unconditionally disabled: this made direct API calls to Claude and
        OpenAI, which is exactly the ~20-package dependency tree (anthropic,
        openai and everything under them) that got removed. Not gated on
        Agent.configured() any more -- that still reads a key someone may
        have saved before this was disabled, and letting that through would
        try to build a chat panel whose provider call immediately fails with
        MissingDependency, since the SDK it needs is no longer installed.
        Claude and ChatGPT reach this app through the MCP server now.
        """
        if self.chat_dock is not None:
            self.removeDockWidget(self.chat_dock)
            self.chat_dock.deleteLater()
            self.chat_dock = self.chat = self.agent = None
            self.statusBar().showMessage("AI assistant disabled.", 5000)

    def _sync_bridge(self):
        """Listen for the MCP process, but only while MCP is enabled.

        Without this the MCP server can read intel but cannot touch the map
        you have open, because it runs in its own process. Turning MCP off
        closes the pipe, so there is nothing listening when the feature is off.
        """
        from ..ai.bridge import BridgeServer
        if config.get_mcp_enabled():
            if self.bridge is None:
                self.bridge = BridgeServer(self)
                if not self.bridge.start():
                    self.bridge = None      # another instance owns the pipe
        elif self.bridge is not None:
            self.bridge.stop()
            self.bridge = None

    def _ask_ai(self, question: str):
        agent = self.agent

        def run(progress=None):
            return agent.ask(question, progress=progress)

        w = Worker(run)
        w.progress.connect(self.chat.set_status)
        w.finished_ok.connect(lambda text: self.chat.add_reply(text, agent.log))
        w.failed.connect(self.chat.add_error)
        self._run(w)

    def run_ai_tool(self, tool, args: dict):
        """Execute one assistant tool call.

        Called from a worker thread. Tools that only read are safe there, but
        anything touching a panel has to land on the UI thread, so those are
        marshalled across and waited on.
        """
        if not tool.writes:
            return tool.fn(self, **args)

        from PySide6.QtCore import QEventLoop, QTimer
        box = {}
        loop = QEventLoop()

        def call():
            try:
                box["out"] = tool.fn(self, **args)
            except Exception as exc:
                box["err"] = exc
            loop.quit()

        QTimer.singleShot(0, call)
        loop.exec()
        if "err" in box:
            raise box["err"]
        return box.get("out", "")

    # -- sovereignty territory ----------------------------------------------
    # Standing colours first: for a route planner the question is not "who
    # owns this" but "do they shoot me", and that is what the contacts data
    # already answers.
    SOV_LABEL_MIN_SYSTEMS = 8

    # Distinct colours to hand out. Only ever a handful are in play at any one
    # border -- a map needs four -- so this is far more than enough to also
    # keep the big blocs looking different from each other across the map.
    SOV_PALETTE = 20
    # The arc of the hue wheel territory may use. Red is left out on purpose:
    # every system inside sovereign space is null-sec, null-sec dots are dark
    # red, and a red territory swallows the very systems it is drawn for. No
    # amount of transparency fixes that -- it moves lightness, and the clash
    # is in hue -- so the security ramp keeps red and the territory palette,
    # which is arbitrary anyway, starts past it.
    SOV_HUE_FROM = 40         # clear of red-orange
    SOV_HUE_SPAN = 285        # up to ~325, stopping short of magenta-red

    @classmethod
    def alliance_colour(cls, slot: int):
        """One entry of the territory palette.

        Hues are spread evenly over the arc rather than stepped by the golden
        angle. The golden angle is only well behaved around a full circle; run
        modulo a partial arc it stops filling the gaps and starts landing
        colours on top of each other -- it put two slots close enough to be
        indistinguishable, which is the exact fault this palette exists to
        avoid. Even spacing has no such failure and guarantees the widest gap
        the arc allows.

        The slots are then handed out on a stride coprime to the count, so
        consecutive slots are still far apart on the wheel -- which is what
        the golden angle was there for -- while every pair stays separated.
        """
        import math

        from PySide6.QtGui import QColor
        n = cls.SOV_PALETTE
        stride = max(1, int(n * 0.382) | 1)
        while math.gcd(stride, n) != 1:
            stride += 2
        hue = cls.SOV_HUE_FROM + cls.SOV_HUE_SPAN * ((slot * stride) % n) / n
        # Alternate value so that even the closest pair of hues separates, and
        # keep saturation high enough to read at SOV_ALPHA over a near-black
        # map.
        return QColor.fromHsv(int(hue), 190, 235 if slot % 2 else 190)

    @classmethod
    def sov_colours(cls, ranked, borders):
        """Pick a colour per alliance so no two that share a border match.

        ``ranked`` is the alliance ids worth colouring, biggest holding first.
        ``borders`` maps an id to the ids it actually touches, which comes out
        of the Voronoi cells rather than being guessed from distance.

        Territory used to be coloured by position in the sorted id list, which
        has nothing to do with where anyone is. That was survivable while
        holdings were islands with black between them -- two alliances sharing
        a colour on opposite sides of the map read as two things. Now that the
        layer fills the box, two same-coloured neighbours share an edge and
        read as one big holding, which is worse than a merely ugly palette: it
        is wrong.

        Biggest first is deliberate. Greedy colouring gives whoever goes first
        the freest choice, and the blocs holding half a region are the ones a
        reader most needs to tell apart; the five-system renters can take
        what is left. Among the colours still open, the least-used one wins,
        so the palette spreads instead of everyone landing on the first few
        slots -- and ties break toward the hue furthest from the neighbours
        already placed, so borders separate as much as the palette allows.
        """
        hues = [(i * 137.508) % 360 for i in range(cls.SOV_PALETTE)]

        def gap(a, b):
            d = abs(a - b) % 360
            return min(d, 360 - d)

        used = [0] * cls.SOV_PALETTE
        slot: dict[int, int] = {}
        for oid in ranked:
            near = [slot[n] for n in borders.get(oid, ()) if n in slot]
            taken = set(near)
            free = [i for i in range(cls.SOV_PALETTE) if i not in taken]
            # Everything adjacent is spoken for. Nothing to do but repeat the
            # least-used colour; with 24 slots this needs a 25-way junction.
            free = free or list(range(cls.SOV_PALETTE))
            slot[oid] = min(free, key=lambda i: (
                used[i], -min((gap(hues[i], hues[n]) for n in near),
                              default=0.0), i))
            used[slot[oid]] += 1
        return {oid: cls.alliance_colour(i) for oid, i in slot.items()}

    def refresh_sov_territory(self):
        """One territory per alliance, filling New Eden.

        Grouping per alliance rather than per standing is a performance
        decision as much as a visual one. Merging the shapes is superlinear in
        subpath count, so folding every neutral system into one cost 1.8 s to
        build and 93 ms a frame to draw; 79 smaller ones are far cheaper, and
        you also get to see each alliance's border.

        Colours are not chosen here. They depend on who borders whom, which is
        only known once the cells are built, so the worker returns the
        adjacency and _on_sov_territory does the picking.
        """
        from PySide6.QtCore import QPointF
        if not (self.map_view and self.universe and self.sov_owners):
            return
        by_alliance: dict[int, list[int]] = {}
        for sid, (owner_id, kind) in self.sov_owners.items():
            if kind != "alliance" or sid not in self.universe.systems:
                continue                    # NPC/faction space is not territory
            by_alliance.setdefault(owner_id, []).append(sid)

        pos = self.map_view._pos
        # Biggest holding first, and it stays that order all the way through:
        # it is the order colours are handed out in, so the blocs a reader
        # most needs to tell apart get the freest pick.
        ranked = sorted(by_alliance, key=lambda oid: (-len(by_alliance[oid]),
                                                      oid))
        payload = []
        for oid in ranked:
            pts = [pos[s] for s in by_alliance[oid] if s in pos]
            if pts:
                payload.append((oid, pts))

        # Name the territories big enough to carry a label. Below this the
        # names collide, and the long tail of tiny holdings collides first.
        # Colour is filled in once it is known.
        biggest = max((len(s) for s in by_alliance.values()), default=1)
        self._sov_labels = []
        for oid in ranked:
            pts = [pos[s] for s in by_alliance[oid] if s in pos]
            name = self.sov_names.get(oid)
            if len(pts) < self.SOV_LABEL_MIN_SYSTEMS or not name:
                continue
            self._sov_labels.append((
                oid, name,
                QPointF(sum(p.x() for p in pts) / len(pts),
                        sum(p.y() for p in pts) / len(pts)),
                len(pts) / biggest))

        # Everything no alliance holds bounds the territory: empire, NPC
        # null-sec, faction space. Testing for held rather than for high-sec
        # is what keeps NPC null out -- it is below 0.0 like sovereign space,
        # so a security test hands it to whichever alliance happens to be
        # nearest, and Curse or Venal ends up painted as somebody's.
        held = {sid for sids in by_alliance.values() for sid in sids}
        outside = [p for sid, p in pos.items() if sid not in held]
        box = self.map_view.universe_box()
        build = self.map_view.build_sov_paths

        w = Worker(lambda: build(payload, box, outside))
        w.finished_ok.connect(self._on_sov_territory)
        w.failed.connect(lambda m: self.statusBar().showMessage(
            f"Sovereignty layer: {m}", 8000))
        self._run(w)

    def _on_sov_territory(self, built):
        """Colour the finished territory and install it.

        ``built`` is [(alliance_id, QPainterPath, bordering ids), ...] in the
        order refresh_sov_territory ranked them: biggest holding first, which
        is the order sov_colours wants.
        """
        if self.map_view is None:
            return
        if not built:
            self.map_view.set_sov_paths(None)
            return
        borders = {oid: touching for oid, _, touching in built}
        colours = self.sov_colours([oid for oid, _, _ in built], borders)
        self.map_view.set_sov_paths(
            [(colours[oid], path) for oid, path, _ in built])
        self.map_view.set_sov_labels(
            [(name, point, colours[oid], weight)
             for oid, name, point, weight in getattr(self, "_sov_labels", ())
             if oid in colours])

    def _standing_bucket(self, owner_id) -> str:
        if self.my_alliance_id and owner_id == self.my_alliance_id:
            return "self"
        standing = self.standings.get(owner_id)
        if standing is None:
            return "unknown"
        if standing > 0:
            return "blue"
        if standing < 0:
            return "red"
        return "neutral"

    # -- system notes -------------------------------------------------------
    def note_for(self, system_id: int) -> str:
        """Your free-text note for a system, or "" if there is none."""
        s = self.universe.systems.get(system_id) if self.universe else None
        return config.get_system_notes().get(s.name, "") if s else ""

    def refresh_notes(self):
        """Re-mark the map after a note is added, edited or cleared."""
        if not (self.map_view and self.universe):
            return
        ids = []
        for name in config.get_system_notes():
            s = self.universe.by_name(name)
            if s is not None:
                ids.append(s.id)
        self.map_view.set_noted(ids)

    def check_cyno_activity(self, system_id: int, done):
        """Look one system up on zKillboard, off the UI thread.

        Calls ``done(text)`` with a line that already carries the caveat, so
        no caller can accidentally present this as a cyno count.
        """
        from ..esi import zkill

        def run(progress=None):
            return zkill.describe(zkill.cyno_losses(system_id, progress=progress))

        w = Worker(run)
        w.progress.connect(lambda m: self.statusBar().showMessage(m, 4000))
        w.finished_ok.connect(done)
        w.failed.connect(lambda m: done(f"zKillboard lookup failed: {m}"))
        self._run(w)

    # -- cyno sweep ---------------------------------------------------------
    def _scan_cynos(self):
        """Sweep killmails region by region for cyno-fitted losses.

        ESI has no cyno data of any kind, so the only observable trace of a
        cyno being lit is the ship that was carrying it turning up on a
        killmail. This walks zKillboard's regional feeds, which embed the
        victim's fitting, and counts those. It is a floor, not a count.
        """
        if self._cyno_worker is not None:
            self._cyno_stop = True
            self.statusBar().showMessage("Stopping cyno scan...", 4000)
            return
        if not self.universe:
            return

        regions = sorted((rid, name) for rid, name
                         in self.universe.region_names.items())
        if not regions:
            return
        hours, ok = QInputDialog.getInt(
            self, "Scan for cyno activity",
            f"Killmail history to scan, in hours.\n\n"
            f"{len(regions)} regions, roughly {len(regions) * 3 // 60 + 1} "
            "minutes. Runs in the background; pick the menu item again to "
            "stop.\n\nCounts ships that died with a cyno fitted, which is a "
            "floor on cyno traffic, not a census of it.",
            24, 1, 168)
        if not ok:
            return

        from ..esi import zkill
        self._cyno_stop = False
        ids = [rid for rid, _ in regions]

        def run(progress=None):
            return zkill.sweep_regions(ids, hours=hours, progress=progress,
                                       should_stop=lambda: self._cyno_stop)

        w = Worker(run)
        w.progress.connect(lambda m: self.statusBar().showMessage(m))
        w.finished_ok.connect(self._on_cyno_sweep)
        w.failed.connect(lambda m: (
            setattr(self, "_cyno_worker", None),
            self.statusBar().showMessage(f"Cyno scan failed: {m}", 10000)))
        self._cyno_worker = w
        self.act_scan_cyno.setText("Stop cyno scan")
        self._run(w)

    def _on_cyno_sweep(self, result):
        from ..esi import zkill
        self._cyno_worker = None
        self.act_scan_cyno.setText("Scan for cyno activity...")
        result = result or {}
        zkill.save_sweep(result)
        self._cyno_sweep = result
        self.cyno_activity = {int(k): int(v)
                              for k, v in (result.get("systems") or {}).items()}
        n, kills = len(self.cyno_activity), result.get("kills", 0)
        part = " (stopped early)" if result.get("partial") else ""
        self.statusBar().showMessage(
            f"Cyno scan: {n} systems with cyno-fitted losses out of "
            f"{kills:,} killmails in {result.get('regions', 0)} regions{part}. "
            "Most cynos are never killed, so this is a floor.", 15000)
        if self._heat_key == "cyno":
            self._set_heat_layer("cyno")

    def _build_docks(self):
        self.ship = ShipSkillsPanel()
        self.route = RoutePanel(self)
        self.character = CharacterPanel()

        d_char = QDockWidget("Character & Structures", self)
        d_char.setWidget(_scrollable(self.character))
        d_ship = QDockWidget("Ship & Skills", self)
        d_ship.setWidget(_scrollable(self.ship))
        d_route = QDockWidget("Route", self)
        d_route.setWidget(_scrollable(self.route))

        # Left column: character (top) with ship config below it (bottom-left).
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, d_char)
        self.splitDockWidget(d_char, d_ship, Qt.Orientation.Vertical)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, d_route)

        # The map is a dock too, so it can be torn onto a second monitor. With
        # no central widget, whatever stays docked fills the window, which is
        # what makes the map expand when the side panels are floated.
        self.map_dock = QDockWidget("Map", self)
        self.map_dock.setObjectName("dock_map")
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.map_dock)
        self.splitDockWidget(self.map_dock, d_route, Qt.Orientation.Horizontal)

        # Panels must never impose a floor on the window: with a minimum width
        # the map cannot reclaim the space when a panel is torn off.
        self._dock_char, self._dock_ship, self._dock_route = d_char, d_ship, d_route
        self._docks = [d_char, d_ship, d_route, self.map_dock]
        for d in self._docks:
            if d.widget():
                d.widget().setMinimumWidth(0)
            d.setMinimumWidth(0)
            d.topLevelChanged.connect(self._on_dock_moved)
            d.visibilityChanged.connect(lambda _=False: self._on_dock_moved())
        self._rebalancing = False

    def reset_panels(self):
        """Put every panel back where it started, floating or hidden alike."""
        self._rebalancing = True
        try:
            for d in self._docks:
                d.setFloating(False)
                d.show()
            # Left column: character above ship. Map centre, route right.
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self._dock_char)
            self.splitDockWidget(self._dock_char, self._dock_ship,
                                 Qt.Orientation.Vertical)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.map_dock)
            self.splitDockWidget(self.map_dock, self._dock_route,
                                 Qt.Orientation.Horizontal)
            if self.chat_dock is not None:
                self.chat_dock.setFloating(False)
                self.chat_dock.show()
                self._dock_chat_at_bottom()
        finally:
            self._rebalancing = False
        self._apply_default_layout()
        self.statusBar().showMessage("Panel layout reset.", 4000)

    def _apply_default_layout(self):
        """Give the map the lion's share of the width.

        Dock widgets divide space by size hint, and the panels ask for far
        more than a QGraphicsView does, so without this the map opens as a
        sliver. Has to run after the map widget is installed, and again on the
        next event-loop turn once real geometry exists.
        """
        from PySide6.QtCore import QTimer

        def apply():
            docks = [d for d in self._docks if not d.isFloating() and d.isVisible()]
            if self.map_dock not in docks:
                return
            widths = [820 if d is self.map_dock else 360 for d in docks]
            self.resizeDocks(docks, widths, Qt.Orientation.Horizontal)

        apply()
        QTimer.singleShot(0, apply)

    def _dock_chat_at_bottom(self):
        """Place the assistant as a short strip across the full width.

        Bottom rather than a third column: the chat panel is read top to
        bottom and the map and route panels are read left to right, so a
        column fight over width was the wrong shape for it from the start.
        Short by default because a question-and-answer exchange does not need
        much height, but not fixed -- resizeDocks sets a starting size the
        splitter handle can still be dragged away from.
        """
        from PySide6.QtCore import QTimer

        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.chat_dock)

        def apply():
            self.resizeDocks([self.chat_dock], [170], Qt.Orientation.Vertical)

        apply()
        QTimer.singleShot(0, apply)

    def _on_dock_moved(self, *_):
        """Keep the window from becoming an empty frame.

        Every panel including the map can float, but if the last docked one
        leaves there is nothing left to drag or drop onto, so the most
        recently detached panel is put straight back.
        """
        if self._rebalancing:
            return
        attached = [d for d in self._docks if not d.isFloating() and d.isVisible()]
        if not attached:
            # The map is the one you actually want on a second monitor, so
            # keep a side panel behind instead of yanking the map back.
            keep = next((d for d in self._docks
                         if d is not self.map_dock and d.isVisible()),
                        self.map_dock)
            self._rebalancing = True
            try:
                keep.setFloating(False)
                keep.show()
            finally:
                self._rebalancing = False
            self.statusBar().showMessage(
                f"{keep.windowTitle()} stays in the main window; "
                "at least one panel has to be docked.", 5000)
            return
        # Re-assert sane widths so a re-attached panel is usable rather than
        # collapsed to a sliver. The map takes whatever is left.
        side = [d for d in attached if d is not self.map_dock]
        if side:
            self.resizeDocks(side, [380] * len(side), Qt.Orientation.Horizontal)

    def _wire(self):
        self.ship.changed.connect(self._on_ship_changed)
        self.route.changed.connect(self._on_route_changed)
        self.route.dotlan_imported.connect(self._on_dotlan_imported)
        self.route.autoroute_requested.connect(self._auto_route)
        self.route.gate_assist_requested.connect(self._gate_assist)
        self.character.login_requested.connect(self._login)
        # Straight to the page it means, rather than to a settings window the
        # user then has to search.
        self.character.scopes_requested.connect(
            lambda: self._open_settings("Permissions"))
        self.character.load_structures_requested.connect(self._load_structures)
        self.character.load_all_structures_requested.connect(
            self._load_all_structures)
        self.character.scan_cyno_requested.connect(self._scan_cyno_alts)
        self.character.force_cyno_requested.connect(
            lambda: self._scan_cyno_alts(force=True))
        self.character.add_system.connect(self.route.add_system)
        self.character.character_changed.connect(self._switch_character)
        self.character.unlink_requested.connect(self._unlink_character)
        self.character.goto_location_requested.connect(self._use_location_as_origin)
        self.character.follow_me_toggled.connect(self._on_follow_me_toggled)
        self.character.chk_follow.setChecked(config.get_follow_me())

    # -- settings persistence ----------------------------------------------
    def _load_settings(self):
        s = config.get_settings()
        if s.get("ship"):
            self.ship.restore(s["ship"])
        if s.get("route"):
            self.route.restore(s["route"])
        view = s.get("view", {})
        layers = view.get("layers", {})
        for key, a in self.act_layers.items():
            # "gate_links" is where this setting lived before the layer menu.
            fallback = view.get("gate_links", True) if key == "gates" else None
            default = fallback if fallback is not None else \
                dict((k, d) for k, _, d, _ in self.MAP_LAYERS)[key]
            a.blockSignals(True)
            a.setChecked(bool(layers.get(key, default)))
            a.blockSignals(False)
        key = view.get("heat", "none")
        if key in self.act_heat:
            self._heat_key = key
            self.act_heat[key].setChecked(True)

    def _save_settings(self):
        if not self._built:
            return
        config.save_settings({
            "ship": self.ship.state(),
            "route": self.route.state(),
            "view": {
                "layers": {k: a.isChecked() for k, a in self.act_layers.items()},
                "heat": self._heat_key,
                # Kept so downgrading to an older build doesn't lose this one.
                "gate_links": self.act_gate_links.isChecked(),
            },
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
        if not self._restored_waypoints_once:
            # Only here, not in _load_settings(): that runs during __init__,
            # before there is a universe to resolve a waypoint's system name
            # against.
            self._restored_waypoints_once = True
            self.route.restore_waypoints(config.get_settings().get("route", {}))
        self.map_view = MapView(universe)
        self.map_view.system_clicked.connect(self._on_map_click)
        self.map_view.system_context.connect(self._map_context)
        for key, a in self.act_layers.items():
            self.map_view.set_overlay_visible(key, a.isChecked())
        universe.set_bridges(config.get_bridges())
        self.map_view.refresh_bridges()
        if self._hole_data:
            # Scouted holes may have landed before the map did; they need a
            # universe to resolve system ids against.
            self._on_wormholes(self._hole_data)
        self.avoided_ids = {s.id for s in
                            (universe.by_name(n) for n in config.get_avoided())
                            if s is not None}
        self.map_view.set_avoided(self.avoided_ids)
        if self.sov_owners:
            self.map_view.set_sov_lookup(self.sov_label)
        self.map_view.set_kill_lookup(self.kills_in)
        self.map_view.set_note_lookup(self.note_for)
        self.refresh_notes()
        if self.kill_activity:
            self.map_view.set_kill_activity(self.kill_activity)
        if self._heat_key != "none":
            # Intel may have arrived before the map finished building.
            self._set_heat_layer(self._heat_key)
        if self.location_system_id:
            # Location may have arrived before the map existed.
            self._on_location({"solar_system_id": self.location_system_id})
        self.map_dock.setWidget(self.map_view)
        self.map_view.setMinimumWidth(0)
        self._apply_default_layout()
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
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.map_dock.setWidget(self._status)
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
        self._sync_location_tracking()

    def _recalc(self):
        if not self.universe or not self.map_view:
            return
        # What the scouted holes are worth depends on the hull and on whether
        # the toggle is on, both of which change here rather than when the
        # connections arrive.
        self._sync_hole_status()
        ship = self.ship.current_ship()
        skills = self.ship.current_skills()
        wps = self.route.systems()
        modes = self.route.modes()
        plan = router.simulate(ship, skills, wps, modes, self.route.strategy(),
                               universe=self.universe)
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
                              self.universe.station_type_names.get,
                              self.has_docking_rights)

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
        # Only offered where there is one: an always-present entry that
        # usually opens an empty dialog teaches people to ignore it.
        holes = self.holes_in(sid)
        act_hole = None
        if holes:
            act_hole = menu.addAction(
                f"Wormhole information ({len(holes)})" if len(holes) > 1
                else "Wormhole information")
        act_info = menu.addAction("Show station info")
        act_wp, wp_actions = self._add_waypoint_menu(menu)
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
        elif act_hole is not None and chosen == act_hole:
            self.show_wormhole_info(sid)
        elif chosen == act_sysinfo:
            self.route.show_system_info(sid)
        elif chosen == act_info:
            self.route.show_station_info(sid)
        elif chosen == act_wp:
            self.set_ingame_waypoint(sid)
        elif chosen in wp_actions:
            self.set_ingame_waypoint(sid, wp_actions[chosen])
        elif chosen == act_avoid:
            self.toggle_avoid(sid)
        elif act_remove is not None and chosen == act_remove:
            self.route.remove_system(sid)

    def holes_in(self, system_id: int) -> list[dict]:
        """Scouted EVE-Scout connections with one end in this system."""
        from ..esi import evescout
        if not self.universe:
            return []
        rows = (self._hole_data or {}).get("rows") or []
        return [c for c in evescout.connections(rows, self.universe.systems)
                if c.get("system_id") == system_id]

    def show_wormhole_info(self, system_id: int):
        from .dialogs import WormholeDialog
        system = self.universe.systems.get(system_id) if self.universe else None
        WormholeDialog(self, system.name if system else str(system_id),
                       self.holes_in(system_id)).exec()

    def add_waypoint_menu(self, menu):
        """Public alias used by the route panel's context menu."""
        return self._add_waypoint_menu(menu)

    def _add_waypoint_menu(self, menu):
        """Add "Set in-game destination" to a menu.

        With one character it is a plain action; with several it becomes a
        submenu so you choose which client receives the destination.
        Returns (single_action_or_None, {action: character_id}).
        """
        targets = self.waypoint_menu_targets()
        if len(targets) <= 1:
            return menu.addAction("Set in-game destination"), {}
        sub = menu.addMenu("Set in-game destination")
        actions = {}
        for cid, name in targets:
            mark = " (active)" if self.token and cid == self.token.character_id else ""
            actions[sub.addAction(f"{name}{mark}")] = cid
        return None, actions

    def waypoint_menu_targets(self):
        """Characters that can receive an in-game waypoint."""
        return sorted((cid, t.character_name) for cid, t in self.tokens.items())

    def set_ingame_waypoint(self, system_id: int, character_id: int | None = None,
                            clear_other_waypoints: bool = False,
                            label: str | None = None, silent: bool = False):
        """Send a destination to one character's client (defaults to active).

        ``system_id`` is what ESI calls destination_id, which despite the
        name also accepts a station or structure id -- so a dock's
        location_id works here exactly the same as a bare system. ``label``
        overrides the display name, needed for exactly that case: a dock id
        cannot be looked up in universe.systems, which only knows systems.

        ``silent`` skips the "no character linked" modal in favour of a
        status-bar line -- for the automatic trigger, where popping a dialog
        with nobody watching to dismiss it would be worse than saying
        nothing happened.
        """
        token = self.tokens.get(character_id) if character_id else self.token
        if not token:
            if silent:
                self.statusBar().showMessage(
                    "Auto-waypoint: no character linked, nothing sent.", 6000)
            else:
                QMessageBox.information(
                    self, "In-game waypoint",
                    "Link a character first (needs the esi-ui.write_waypoint.v1 scope).")
            return
        client = (self.esi if (self.token and token.character_id == self.token.character_id)
                  else EsiClient(token, config.get_client_id()))
        if client is None:
            client = EsiClient(token, config.get_client_id())
        if label is None:
            label = self.universe.systems[system_id].name if (
                self.universe and system_id in self.universe.systems) else str(system_id)
        label = f"{label} ({token.character_name})"
        w = Worker(client.set_waypoint, system_id,
                  clear_other_waypoints=clear_other_waypoints)
        w.finished_ok.connect(
            lambda _: self.statusBar().showMessage(f"Set in-game destination: {label}", 5000))
        w.failed.connect(lambda m: (self.statusBar().showMessage(
            f"In-game waypoint failed: {m}", 8000) if silent else
            QMessageBox.warning(self, "In-game waypoint", m)))
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
                   use_wormholes=self.route.use_wormholes(),
                   haven=self._haven_predicate(ship),
                   jammed=self.jammed_systems(), danger=self.danger_predicate(),
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

    def _haven_predicate(self, ship):
        """Systems where this hull has somewhere safe to sit out the
        reactivation timer: a usable dock, any tetherable structure, or a corp
        POS shield. Used as a soft preference, not a hard filter."""
        from ..data import docking
        cat = docking.ship_category(ship)
        havens: set[int] = set(self.starbase_systems)
        for d in self.dockables:
            if d.kind == "structure" and docking.can_tether_at(d.type_id):
                havens.add(d.solar_system_id)
        if cat != docking.SUPERCAP:
            # Supers cannot use NPC stations at all; everyone else can.
            for sid, stations in self.universe.system_stations.items():
                for st in stations:
                    if docking.check_npc_station(ship, st.type_name,
                                                 st.max_volume).can_dock:
                        havens.add(sid)
                        break
        return lambda system_id: system_id in havens

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
                # Configured docking rights count as safe regardless of the
                # owner's standing.
                safe = chk.safe or self.has_docking_rights(getattr(d, "owner_id", 0))
                if chk.can_dock and (safe or not safe_only):
                    allowed.add(d.solar_system_id)
                elif docking.can_tether_at(d.type_id):
                    # A capital that cannot dock can still tether here, which
                    # beats landing in a system with nothing at all.
                    allowed.add(d.solar_system_id)
        # Corp POS shields are a valid place to park a capital too.
        allowed.update(self.starbase_systems)
        return lambda s: s.id in allowed

    # -- ESI ----------------------------------------------------------------
    def _set_client_id(self):
        from .dialogs import EsiSetupDialog
        dlg = EsiSetupDialog(self, config.get_custom_client_id(),
                             config.REDIRECT_URI, config.get_scopes())
        if dlg.exec() and dlg.client_id():
            cfg = config.load_config()
            cfg["client_id"] = dlg.client_id()
            config.save_config(cfg)
            QMessageBox.information(self, "Saved", "Client ID saved.")

    def _open_settings(self, start_tab: str | None = None):
        """One window for everything that used to be its own menu entry.

        The pages are the existing dialogs reparented into tabs, so each still
        reports what the user chose through the same accessors it always had,
        and the apply steps below are the same code that used to run behind
        each dialog's own OK button.
        """
        from ..esi import intel_store
        from .ai_dialog import AiSettingsDialog
        from .dialogs import (
            AnsiblexDialog,
            AvoidDialog,
            DockingRightsDialog,
            EsiSetupDialog,
            IntelSettingsDialog,
            WandererDialog,
        )
        from .settings import AppearancePage, ScopesPage, SettingsDialog

        dlg = SettingsDialog(self, start_tab)

        esi_page = dlg.add_page("EVE account", EsiSetupDialog(
            dlg, config.get_custom_client_id(), config.REDIRECT_URI,
            config.get_scopes()), scroll=True)
        scopes_page = dlg.add_page("Permissions", ScopesPage(dlg), scroll=True)

        avoid_names = sorted(
            self.universe.systems[i].name for i in self.avoided_ids
            if i in self.universe.systems) if self.universe else \
            config.get_avoided()
        avoid_page = dlg.add_page("Avoided systems", AvoidDialog(dlg, avoid_names))

        bridge_page = dlg.add_page("Ansiblex", AnsiblexDialog(
            dlg, config.get_bridges()))
        bridge_page.btn_esi.clicked.connect(
            lambda: self._load_ansiblex_esi(bridge_page))
        bridge_page.btn_search.clicked.connect(
            lambda: self._search_ansiblex(bridge_page))
        bridge_page.search_field.returnPressed.connect(
            lambda: self._search_ansiblex(bridge_page))

        wanderer_page = dlg.add_page("Wanderer", WandererDialog(
            dlg, config.get_wanderer_url(), config.get_wanderer_map(),
            config.get_wanderer_token()))

        rights_page = dlg.add_page("Docking rights", DockingRightsDialog(
            dlg, config.get_docking_rights()))

        intel_page = dlg.add_page("Intel", IntelSettingsDialog(
            dlg, config.get_intel_refresh_minutes()))
        intel_page.set_history_days(config.get_intel_history_days())
        intel_page.set_stats(intel_store.stats())

        def purge():
            if QMessageBox.question(
                    dlg, "Delete history",
                    "Delete all stored intel history? This cannot be undone."
            ) == QMessageBox.StandardButton.Yes:
                intel_store.purge()
                intel_page.set_stats(intel_store.stats())

        intel_page.btn_purge.clicked.connect(purge)

        ai_page = dlg.add_page("Assistant", AiSettingsDialog(dlg), scroll=True)
        appearance = dlg.add_page("Appearance", AppearancePage(dlg))

        if not dlg.exec():
            return

        # One summary at the end. Applying nine pages one at a time the way
        # each dialog used to would queue up a stack of message boxes.
        notes: list[str] = []
        for step, page in ((self._apply_client_id, esi_page),
                           (self._apply_scopes, scopes_page),
                           (self._apply_avoided, avoid_page),
                           (self._apply_bridges, bridge_page),
                           (self._apply_wanderer, wanderer_page),
                           (self._apply_docking_rights, rights_page),
                           (self._apply_intel, intel_page),
                           (self._apply_ai, ai_page),
                           (self._apply_appearance, appearance)):
            try:
                step(page, notes)
            except Exception as exc:                      # noqa: BLE001
                # One bad page must not abandon the other eight half-applied.
                notes.append(f"{step.__name__[7:]}: {exc}")
        if notes:
            QMessageBox.information(self, "Settings", "\n".join(notes))

    # -- one apply per page, each the code its own dialog used to run -------
    def _apply_client_id(self, page, notes):
        cid = page.client_id()
        if cid and cid != config.get_custom_client_id():
            cfg = config.load_config()
            cfg["client_id"] = cid
            config.save_config(cfg)
            notes.append("Client ID saved.")

    def _apply_scopes(self, page, notes):
        wanted = page.scopes()
        if wanted != config.get_scopes():
            config.set_scopes(wanted)
            notes.append(f"Requesting {len(wanted)} scope(s). Sign in again "
                         "for that to take effect.")

    def _apply_avoided(self, page, notes):
        if not self.universe:
            return
        ids, bad = set(), []
        for n in page.names():
            s = self.universe.by_name(n)
            (ids.add(s.id) if s else bad.append(n))
        if ids == self.avoided_ids and not bad:
            return
        self.avoided_ids = ids
        config.set_avoided([self.universe.systems[i].name for i in ids])
        if self.map_view:
            self.map_view.set_avoided(ids)
        msg = f"Avoiding {len(ids)} system(s)."
        if bad:
            msg += " Unknown: " + ", ".join(bad[:6])
        notes.append(msg)

    def _apply_bridges(self, page, notes):
        pairs = page.pairs()
        if not self.universe:
            return
        resolved = self.universe.set_bridges(pairs)
        bad = len(pairs) - len(resolved)
        config.set_bridges(resolved)
        if self.map_view:
            self.map_view.refresh_bridges()
        msg = f"{len(resolved)} Ansiblex link(s) active."
        if bad:
            msg += f" {bad} line(s) ignored - unknown system name."
        notes.append(msg)
        self._recalc()

    def _apply_wanderer(self, page, notes):
        values = list(page.values())
        if values != [config.get_wanderer_url(), config.get_wanderer_map(),
                      config.get_wanderer_token()]:
            config.set_wanderer(*values)
            # Settings changed, so any cached map is for the wrong instance.
            self._wanderer_data = {}
            self._fetch_wanderer(force=True)
            notes.append("Wanderer settings saved; refreshing the map.")

    def _apply_docking_rights(self, page, notes):
        names = page.names()
        if names == config.get_docking_rights():
            return
        config.set_docking_rights(names)
        self._resolve_docking_rights(
            names, lambda unknown: self.statusBar().showMessage(
                f"{len(names) - len(unknown)} of {len(names)} docking-rights "
                "name(s) resolved.", 8000))
        notes.append(f"{len(names)} docking-rights name(s) saved; resolving.")

    def _apply_intel(self, page, notes):
        config.set_intel_refresh_minutes(page.minutes())
        config.set_intel_history_days(page.history_days())
        self._start_intel_timer()
        if page.refresh_now():
            self.refresh_intel()

    def _apply_ai(self, page, notes):
        """MCP settings only now -- the in-app chat box on this page is
        disabled and page.save() no longer writes anything for it. No note
        logic here any more either: with the chat dock permanently unbuilt
        (see _sync_chat_panel), every case that used to distinguish "opened"
        from "closed" from "should have opened but the package was missing"
        collapses to the same, permanent, unreachable-condition no-op.
        """
        for name in ("apply", "save", "commit"):
            fn = getattr(page, name, None)
            if callable(fn):
                fn()
                break
        self._sync_bridge()
        self._sync_chat_panel()

    def _apply_appearance(self, page, notes):
        from .theme import get_chrome, set_chrome
        mode = "native" if page.native() else "dark"
        if mode != get_chrome():
            set_chrome(mode)
            notes.append(f"{mode.capitalize()} chrome will be used when you "
                         "restart Eve-Strait.")

    def _set_chrome(self, native: bool):
        """Saved, then applied on restart.

        Switching live would only get half of it: the panels bake a few colours
        into inline stylesheets when they are built, and those would keep their
        dark values on a freshly light window - unreadable, and worse than not
        having switched at all.
        """
        from .theme import set_chrome
        set_chrome("native" if native else "dark")
        QMessageBox.information(
            self, "Window chrome",
            ("Native chrome will be used when you restart Eve-Strait.\n\n"
             "The panels will follow your operating system theme, including "
             "any high-contrast or forced-colour settings."
             if native else
             "The dark theme will be used when you restart Eve-Strait.")
            + "\n\nThe map keeps its own colours either way - it is a chart "
              "rather than chrome.")

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
                   use_wormholes=self.route.use_wormholes(),
                   haven=self._haven_predicate(ship),
                   jammed=self.jammed_systems(), danger=self.danger_predicate(),
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
        """Link a character. Existing characters are kept; this one becomes active."""
        auth.save(token)
        self.tokens[token.character_id] = token
        self.token = token
        self.esi = EsiClient(token, config.get_client_id())
        self.character.btn_login.setEnabled(True)
        self._refresh_character_list()
        self._fetch_contacts()
        self._fetch_starbases()
        self._fetch_location()
        self._fetch_ship_type()
        self._sync_location_tracking()

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
                     "matches, then Settings → Permissions and retry.")
        QMessageBox.warning(self, "Login failed", msg + extra)

    def _logout(self):
        """Unlink every character."""
        if self.tokens and QMessageBox.question(
                self, "Log out",
                f"Unlink all {len(self.tokens)} character(s)?"
        ) != QMessageBox.StandardButton.Yes:
            return
        auth.logout()
        self.tokens = {}
        self.token = None
        self.esi = None
        self.dockables = []
        self.standings = {}
        self.starbase_systems = {}
        self.location_system_id = None
        self.character.set_dockables([])
        self.character.set_location("")
        self._sync_location_tracking()
        self._refresh_character_list()
        self._render_character()

    def _scan_cyno_alts(self, force: bool = False):
        """Roll-call of which linked characters can light a cyno, and where."""
        if not self.tokens:
            QMessageBox.information(self, "Cyno alts", "Link a character first.")
            return
        if not self.universe or not self.universe.cyno_modules:
            QMessageBox.information(
                self, "Cyno alts",
                "The type data has not finished loading yet. Try again in a "
                "moment.")
            return
        from ..esi import client as _client
        self.character.set_cyno_scanning(True)
        cid = config.get_client_id()
        mods = self.universe.cyno_modules
        tokens = dict(self.tokens)
        w = Worker(lambda progress=None: _client.scan_cyno_alts(
            tokens, cid, mods, progress, force=force))
        w.finished_ok.connect(self._on_cyno_alts)
        w.failed.connect(lambda m: (self.character.set_cyno_scanning(False),
                                    QMessageBox.warning(self, "Cyno alts", m)))
        self._run(w, "Scanning characters for cynos…")

    def _on_cyno_alts(self, result):
        alts, notes = result
        self.cyno_alts = alts
        self.character.set_cyno_scanning(False)
        from ..esi.transport import get_transport
        if self.token:
            st = get_transport().cache_status(
                f"/characters/{self.token.character_id}/assets/",
                params={"page": 1}, character_id=self.token.character_id)
            self.character.set_cyno_freshness(
                st.fetched_at if st else None,
                st.expires_at if st else None)
        self.character.set_cyno_alts(alts, notes, self._system_name)
        if self.map_view:
            self.map_view.set_cyno_alts(alts)

    def _system_name(self, system_id: int) -> str:
        if self.universe:
            s = self.universe.systems.get(int(system_id))
            if s:
                return s.name
        return str(system_id)

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
        self._run(w, "Loading dockable structures…")

    def _load_all_structures(self):
        """Fill every linked character's dockables cache in one pass."""
        if not self.tokens:
            QMessageBox.information(self, "Structures", "Log in first.")
            return
        from ..esi import client as _client
        self.character.set_loading(True, "Loading assets…")
        cid = config.get_client_id()
        tokens = dict(self.tokens)
        w = Worker(lambda progress=None: _client.load_all_dockables(
            tokens, cid, progress))
        w.progress.connect(lambda msg: self.character.set_loading(True, msg))
        w.finished_ok.connect(self._on_all_structures)
        w.failed.connect(lambda m: (self.character.set_loading(False),
                                    QMessageBox.warning(self, "Structures", m)))
        self._run(w, "Loading dockables for all characters…")

    def _on_all_structures(self, result):
        results, notes = result
        self.character.set_loading(False)
        # The active character's list is what routing uses; re-read it from
        # the cache the bulk load just wrote.
        self._load_cached_dockables()
        self._render_character()
        self.route.refresh()
        total = sum(len(v) for v in results.values())
        self.statusBar().showMessage(
            f"Loaded {total} dockable locations across {len(results)} "
            f"character(s).", 8000)
        if notes:
            QMessageBox.information(self, "Structures", "\n".join(notes))

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
    def _run(self, worker: Worker, label: str = ""):
        """Start a background worker and show it in the status bar.

        Every background operation in the app funnels through here, so this is
        the one place that needs to know a task started -- the workers
        themselves stay unaware of the UI.
        """
        self._workers.append(worker)
        key = id(worker)
        self._tasks.add(key, label)
        self._sync_busy()
        # Workers already emit progress text; route it to the indicator so a
        # long job says what it is doing rather than just spinning.
        worker.progress.connect(lambda msg, k=key: self._task_progress(k, msg))
        worker.finished.connect(lambda k=key: self._task_finished(k))
        worker.finished.connect(
            lambda: self._workers.remove(worker) if worker in self._workers else None)
        worker.start()

    def _task_progress(self, key, message: str):
        self._tasks.update(key, message)
        self._sync_busy()

    def _task_finished(self, key):
        self._tasks.remove(key)
        self._sync_busy()

    def _sync_busy(self):
        if self._busy is not None:
            self._busy.set_state(self._tasks.summary(), self._tasks.tooltip())
