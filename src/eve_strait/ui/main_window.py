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
        self.setWindowTitle("Eve-Strait")
        self.resize(1500, 950)

        self.universe: Universe | None = None
        self.map_view: MapView | None = None
        self.dockables: list = []
        self.tokens: dict[int, auth.Token] = auth.load_all()
        self.token: auth.Token | None = auth.load_saved()
        self.esi: EsiClient | None = None
        self.location_system_id: int | None = None
        self.kill_activity: dict[int, dict] = {}
        self.jump_activity: dict[int, int] = {}
        self.activity_totals: dict = {"jumps": {}, "kills": {}, "hours": 0}
        self.sov_defense: dict[int, dict] = {}
        self.industry_index: dict[int, dict] = {}
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
        self.starbase_systems: dict[int, int] = {}
        self.sov_owners: dict[int, tuple] = {}
        self.sov_names: dict[int, str] = {}
        self._built = False

        self._status = QLabel("Loading New Eden map data...")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._build_menu()
        self._build_docks()
        self._load_settings()
        self._wire()
        self._built = True

        self._refresh_character_list()
        self._load_cached_dockables()
        if self.token:
            self._fetch_contacts()
            self._fetch_starbases()
            self._fetch_location()
        self._fetch_incursions()
        self.refresh_intel()
        self._start_intel_timer()
        self._fetch_sovereignty()
        self._resolve_docking_rights()
        from .. import update as _upd
        if _upd.auto_check_enabled() and _upd.is_frozen():
            self._check_updates()
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
        self._refresh_character_list()
        self._render_character()

    # -- current location ---------------------------------------------------
    def _fetch_location(self):
        """Where the active character is (scope already granted at login)."""
        if not self.token:
            return
        if not self.esi:
            self.esi = EsiClient(self.token, config.get_client_id())
        w = Worker(self.esi.location)
        w.finished_ok.connect(self._on_location)
        w.failed.connect(lambda m: self.character.set_location(""))
        self._run(w)

    def _on_location(self, data):
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
    def _fetch_activity(self):
        from ..esi import client
        w = Worker(client.system_activity)
        w.finished_ok.connect(self._on_activity)
        w.failed.connect(lambda m: None)
        self._run(w)

    def refresh_intel(self):
        """Re-poll every activity source: kills, traffic, ADM, industry."""
        self._fetch_activity()
        self._fetch_defense()

    def _start_intel_timer(self):
        """Re-poll on the configured interval. 0 minutes means never."""
        from PySide6.QtCore import QTimer
        if getattr(self, "_intel_timer", None) is None:
            self._intel_timer = QTimer(self)
            self._intel_timer.timeout.connect(self.refresh_intel)
        minutes = config.get_intel_refresh_minutes()
        if minutes <= 0:
            self._intel_timer.stop()
        else:
            self._intel_timer.start(minutes * 60 * 1000)

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

    def _fetch_defense(self):
        """ADM and industry indices: the 'is anyone actually here' signals."""
        from ..esi import client
        def arrived(attr, key):
            def slot(d):
                setattr(self, attr, d or {})
                if self._heat_key == key:
                    self._set_heat_layer(key)
            return slot

        w = Worker(client.sovereignty_defense)
        w.finished_ok.connect(arrived("sov_defense", "adm"))
        w.failed.connect(lambda m: None)
        self._run(w)
        w2 = Worker(client.industry_indices)
        w2.finished_ok.connect(arrived("industry_index", "industry"))
        w2.failed.connect(lambda m: None)
        self._run(w2)

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
            if self._heat_key == "jumps_24h":
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
            ("Intel refresh & history...", self._edit_intel_settings),
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
        ("ship_kills", "Player ship kills, last hour", "Where people are dying."),
        ("pod_kills", "Pod kills, last hour", "Podded, so a fight went badly."),
        ("adm", "Sovereignty ADM",
         "Activity Defense Multiplier, 1 to 6. Raised by ratting, mining and "
         "industry in the system, so a high ADM means a used system."),
        ("industry", "Industry cost index",
         "Manufacturing cost index. Rises with local production."),
    )

    def _toggle_layer(self, key: str, visible: bool):
        if self.map_view:
            self.map_view.set_overlay_visible(key, visible)
        self._save_settings()

    def _set_all_layers(self, on: bool):
        for key, a in self.act_layers.items():
            a.blockSignals(True)
            a.setChecked(on)
            a.blockSignals(False)
            if self.map_view:
                self.map_view.set_overlay_visible(key, on)
        self._save_settings()

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
        if key == "jumps_1h":
            values = dict(self.jump_activity or {})
        elif key == "jumps_24h":
            values = dict(totals.get("jumps") or {})
            hours = totals.get("hours", 0)
            label = f"{label} ({hours}h so far)" if hours < 24 else label
        elif key in ("ship_kills", "pod_kills", "npc_kills"):
            field = key.split("_")[0]
            values = {sid: c.get(field, 0) for sid, c in kills.items()}
        elif key == "adm":
            values = {sid: d.get("adm") or 0
                      for sid, d in (self.sov_defense or {}).items()}
        elif key == "industry":
            values = {sid: (d.get("manufacturing") or 0) * 100
                      for sid, d in (self.industry_index or {}).items()}
        else:
            values = {}

        if not values:
            self._status.setText(
                f"No {label.lower()} data yet. It arrives with the next intel "
                "refresh (File → Intel refresh & history).")
        self.map_view.set_heat(values, label)

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
        self.route.autoroute_requested.connect(self._auto_route)
        self.route.gate_assist_requested.connect(self._gate_assist)
        self.character.login_requested.connect(self._login)
        self.character.load_structures_requested.connect(self._load_structures)
        self.character.add_system.connect(self.route.add_system)
        self.character.character_changed.connect(self._switch_character)
        self.character.unlink_requested.connect(self._unlink_character)
        self.character.goto_location_requested.connect(self._use_location_as_origin)

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
        self.map_view = MapView(universe)
        self.map_view.system_clicked.connect(self._on_map_click)
        self.map_view.system_context.connect(self._map_context)
        for key, a in self.act_layers.items():
            self.map_view.set_overlay_visible(key, a.isChecked())
        universe.set_bridges(config.get_bridges())
        self.map_view.refresh_bridges()
        self.avoided_ids = {s.id for s in
                            (universe.by_name(n) for n in config.get_avoided())
                            if s is not None}
        self.map_view.set_avoided(self.avoided_ids)
        if self.sov_owners:
            self.map_view.set_sov_lookup(self.sov_label)
        self.map_view.set_kill_lookup(self.kills_in)
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

    def set_ingame_waypoint(self, system_id: int, character_id: int | None = None):
        """Send a destination to one character's client (defaults to active)."""
        token = self.tokens.get(character_id) if character_id else self.token
        if not token:
            QMessageBox.information(
                self, "In-game waypoint",
                "Link a character first (needs the esi-ui.write_waypoint.v1 scope).")
            return
        client = (self.esi if (self.token and token.character_id == self.token.character_id)
                  else EsiClient(token, config.get_client_id()))
        if client is None:
            client = EsiClient(token, config.get_client_id())
        name = self.universe.systems[system_id].name if self.universe else system_id
        name = f"{name} ({token.character_name})"
        w = Worker(client.set_waypoint, system_id)
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
        self._refresh_character_list()
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
