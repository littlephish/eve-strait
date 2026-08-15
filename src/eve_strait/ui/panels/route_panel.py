"""Route building: search, waypoints, dock picker, options and results."""
from __future__ import annotations

from itertools import count

from PySide6.QtCore import QStringListModel, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
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

from ..collapsible import Section
from ..theme import TEXT_MUTED, WARN, compressible, pad, shrinkable
from ..models import DockOption, Waypoint, docks_for_system, effective_dock

_ROLE_SYS = Qt.ItemDataRole.UserRole
_ROLE_UID = Qt.ItemDataRole.UserRole + 1
_STATUS_ICON = {True: "✓", False: "✗"}


class RoutePanel(QWidget):
    _HOLES_TIP = ("Route through the public Thera and Turnur wormholes scouted "
                  "by EVE-Scout.\n"
                  "Holes too small for the selected hull are ignored.\n"
                  "Connections are scanned by volunteers and expire within "
                  "hours — check before you commit.")

    changed = Signal()
    autoroute_requested = Signal()
    gate_assist_requested = Signal()
    dotlan_imported = Signal(object)     # data.dotlan.DotlanRoute

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.waypoints: list[Waypoint] = []
        self.route_modes: list[str] = []
        self._uid = count(1)
        self._uid_map: dict[int, Waypoint] = {}

        v = pad(QVBoxLayout(self))

        # -- search ---------------------------------------------------------
        sbox = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find system by name...")
        self.search.returnPressed.connect(self._do_search)
        self.search.textEdited.connect(self._on_search_text)
        b_find = QPushButton("Find")
        b_find.clicked.connect(self._do_search)
        sbox.addWidget(self.search, 1)
        sbox.addWidget(b_find, 0)
        v.addLayout(sbox)

        # Results come up in a completer popup rather than a list inside the
        # panel. An inline list has to appear and disappear as you type, and
        # every time it does everything below it jumps -- the waypoints, the
        # options, the plan. The popup floats above the panel, so nothing
        # moves, and it costs no vertical space when it is not open.
        self._completer_model = QStringListModel()
        self.completer = QCompleter(self._completer_model, self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setCompletionMode(
            QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.completer.setMaxVisibleItems(12)
        self.completer.activated[str].connect(self._on_completed)
        self.search.setCompleter(self.completer)

        hint = QLabel("Type to search, or click the map, to add a waypoint. "
                      "Drag to reorder; right-click to remove. First = origin.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{TEXT_MUTED}")
        v.addWidget(hint)

        # -- waypoints ------------------------------------------------------
        v.addWidget(QLabel("Waypoints"))
        self.wp_list = QListWidget()
        self.wp_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.wp_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.wp_list.customContextMenuRequested.connect(self._wp_menu)
        self.wp_list.itemSelectionChanged.connect(self._on_select)
        self.wp_list.model().rowsMoved.connect(self._on_rows_moved)
        # Stretch rather than natural size: the waypoint list and the plan are
        # what the panel is for, and everything between them is settings.
        v.addWidget(self.wp_list, 3)

        row = QHBoxLayout()
        for text, slot in (("↑", self._up), ("↓", self._down),
                           ("Remove", self._remove_selected), ("Clear", self._clear)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(compressible(b, 40))
        v.addLayout(row)

        # On its own row: five buttons abreast each demanded their full label
        # width, which set the panel's minimum to 652px and pushed everything
        # to the right of it off the dock.
        self.b_auto = QPushButton("Auto-route")
        self.b_auto.setToolTip("Fill in the systems needed between the first "
                               "and last waypoint.")
        self.b_auto.clicked.connect(self.autoroute_requested)
        v.addWidget(compressible(self.b_auto, 80))

        self.busy = QProgressBar()
        self.busy.setRange(0, 0)          # indeterminate spinner
        self.busy.setFormat("Finding route…")
        self.busy.hide()
        v.addWidget(self.busy)

        # -- options, grouped and collapsed ----------------------------------
        # Nineteen controls in one flat column made the panel a wall. They are
        # the same controls, sorted by the question they answer, with the
        # current state summarised in each header so collapsing hides the
        # widgets without hiding what they are set to.
        sec_jumps = Section("Ship & jumps")
        sec_safety = Section("Safety")
        sec_dock = Section("Docking")
        self._sections = (sec_jumps, sec_safety, sec_dock)

        # -- dock picker (change which dock) --------------------------------
        self.lbl_dock = QLabel("Dock:")
        self.cmb_pick = QComboBox()
        shrinkable(self.cmb_pick)
        self.cmb_pick.currentIndexChanged.connect(self._on_pick)
        pick_row = QHBoxLayout()
        pick_row.addWidget(self.lbl_dock)
        pick_row.addWidget(self.cmb_pick, 1)
        sec_dock.add(pick_row)

        # -- options --------------------------------------------------------
        opt = QHBoxLayout()
        opt.addWidget(QLabel("Docking:"))
        self.cmb_policy = QComboBox()
        self.cmb_policy.addItems(
            ["No docking filter", "Require any docking", "Prefer safe docking only"])
        shrinkable(self.cmb_policy)
        self.cmb_policy.currentIndexChanged.connect(self._emit_changed)
        opt.addWidget(self.cmb_policy, 1)
        sec_dock.add(opt)

        self.chk_nodocks = QCheckBox("Just passing through")
        self.chk_nodocks.setToolTip(
            "For subcaps and freighters warping gate to gate. Waypoints stop "
            "naming a station, and the dock picker is disabled. The docking "
            "filter above still applies to routing.")
        compressible(self.chk_nodocks)
        self.chk_nodocks.toggled.connect(self._on_nodocks_toggled)
        sec_dock.add(self.chk_nodocks)

        self.chk_gates = QCheckBox("Allow gates")
        self.chk_gates.setChecked(True)
        self.chk_gates.setToolTip(
            "Use stargates wherever they save jumps - regional gates that span "
            "further than you can jump, and gating out of hi-sec to a jumpable "
            "system. Unchecked = jump drive only.")
        compressible(self.chk_gates)
        self.chk_gates.toggled.connect(self._on_gates_toggled)
        sec_jumps.add(self.chk_gates)

        opt2 = QHBoxLayout()
        opt2.addWidget(QLabel("     Balance:"))
        self.cmb_balance = QComboBox()
        # Label -> cost of one jump measured in gate hops. Below 1 means a
        # jump is cheaper than a single gate hop, i.e. "gate only when it
        # genuinely buys something" -- which is the usual capital preference.
        for label, cost in (
            ("Jump always", 0.3),
            ("Prefer jumps", 0.6),
            ("Balanced", 1.5),
            ("Prefer gates", 6.0),
            ("Gate always", 30.0),
        ):
            self.cmb_balance.addItem(label, cost)
        self.cmb_balance.setCurrentIndex(1)
        self.cmb_balance.setToolTip(
            "How eagerly gates are used instead of jumps.\n"
            "Jump-heavy settings are fast and keep you off gates; gate-heavy\n"
            "settings save fuel and fatigue but mean long gate chains.\n"
            "A regional gate that spans further than you can jump is taken at\n"
            "any setting, because no number of jumps replaces it.")
        shrinkable(self.cmb_balance)
        self.cmb_balance.currentIndexChanged.connect(self._emit_changed)
        opt2.addWidget(self.cmb_balance, 1)
        sec_jumps.add(opt2)

        self.chk_ansiblex = QCheckBox("Use Ansiblex network")
        self.chk_ansiblex.setChecked(True)
        self.chk_ansiblex.setToolTip(
            "Route through your configured Ansiblex jump gates "
            "(Settings → Ansiblex).")
        compressible(self.chk_ansiblex)
        self.chk_ansiblex.toggled.connect(self._emit_changed)
        sec_jumps.add(self.chk_ansiblex)

        # Off by default: these are volunteer-scanned connections that expire
        # in hours, so opting in should be deliberate rather than something
        # that quietly reroutes a freighter through a hole that has collapsed.
        self.chk_holes = QCheckBox("EVE-Scout wormholes")
        self.chk_holes.setChecked(False)
        self.chk_holes.setToolTip(
            "Route through the public Thera and Turnur wormholes scouted by "
            "EVE-Scout.\nHoles too small for the selected hull are ignored.\n"
            "Connections are scanned by volunteers and expire within hours — "
            "check before you commit.")
        compressible(self.chk_holes)
        self.chk_holes.toggled.connect(self._emit_changed)
        sec_jumps.add(self.chk_holes)
        # Filled in by set_hole_status() once the connections are fetched.
        self.lbl_holes = QLabel("")
        self.lbl_holes.setWordWrap(True)
        self.lbl_holes.setVisible(False)
        sec_jumps.add(self.lbl_holes)

        opt3 = QHBoxLayout()
        self.cmb_gate = QComboBox()
        self.cmb_gate.addItems(["Fastest", "Safer", "Less secure"])
        self.cmb_gate.setToolTip(
            "Fastest: fewest hops.\n"
            "Safer: prefer high-sec.\n"
            "Less secure: prefer low and null.")
        shrinkable(self.cmb_gate)
        self.cmb_gate.currentIndexChanged.connect(self._emit_changed)
        self.cmb_gate.currentIndexChanged.connect(
            lambda _=0: self._sync_hole_toggle())
        # NOT a second Auto-route button: self.b_auto already exists (the one
        # actually in the layout, above, next to the waypoint list) and this
        # used to silently reassign the name to an orphaned QPushButton that
        # was never added anywhere. It stayed clickable-looking nowhere and,
        # worse, meant set_busy()'s self.b_auto.setEnabled() below was toggling
        # that invisible button instead of the real one -- so the visible
        # Auto-route button never actually disabled while a route computed.
        opt3.addWidget(QLabel("Gates:"))
        opt3.addWidget(self.cmb_gate, 1)
        sec_jumps.add(opt3)
        self._sync_hole_toggle()      # cmb_gate exists only from here on

        self.chk_auto_waypoint = QCheckBox("Auto-set in-game waypoint after my last jump")
        self.chk_auto_waypoint.setToolTip(
            "For a JF or capital finishing a route on gates: the moment your "
            "tracked location reaches the system where your last jump, "
            "bridge or wormhole hop lands, this sets the in-game autopilot "
            "destination to the final waypoint (its chosen dock, if one is "
            "set) so the rest of the trip flies itself. Needs a linked "
            "character with the esi-ui.write_waypoint.v1 scope. Fires once "
            "per route.")
        compressible(self.chk_auto_waypoint)
        self.chk_auto_waypoint.toggled.connect(self._emit_changed)
        self.chk_auto_waypoint.toggled.connect(self._sync_auto_waypoint_label)
        sec_jumps.add(self.chk_auto_waypoint)

        self.lbl_auto_waypoint = QLabel("")
        self.lbl_auto_waypoint.setWordWrap(True)
        self.lbl_auto_waypoint.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
        self.lbl_auto_waypoint.setVisible(False)
        sec_jumps.add(self.lbl_auto_waypoint)

        self.chk_reactivation = QCheckBox("Minimize reactivation timer")
        compressible(self.chk_reactivation)
        self.chk_reactivation.toggled.connect(self._emit_changed)
        sec_safety.add(self.chk_reactivation)

        self.chk_hostile = QCheckBox("Exclude hostile structures")
        compressible(self.chk_hostile)
        self.chk_hostile.toggled.connect(self._emit_changed)
        sec_safety.add(self.chk_hostile)

        self.chk_incursions = QCheckBox("Avoid incursion systems")
        compressible(self.chk_incursions)
        self.chk_incursions.toggled.connect(self._emit_changed)
        sec_safety.add(self.chk_incursions)

        self.chk_kills = QCheckBox("Steer around recent kills")
        self.chk_kills.setToolTip(
            "Bias the route away from systems with player kills in the last "
            "hour. A preference, not a hard block: a route is never made "
            "impossible by it.")
        compressible(self.chk_kills)
        self.chk_kills.toggled.connect(self._emit_changed)
        sec_safety.add(self.chk_kills)


        for _s in self._sections:
            v.addWidget(_s)

        # -- results --------------------------------------------------------
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Mode", "From", "To", "LY", "Fuel", "Reactivate", "Fatigue", "OK"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.table, 2)

        self.totals = QLabel("Add 2+ waypoints to plan a route.")
        self.totals.setWordWrap(True)
        self.totals.setStyleSheet("font-weight:bold")
        v.addWidget(self.totals)

        act_row = QHBoxLayout()
        b_assist = QPushButton("Gates…")
        b_assist.setToolTip("Compare a pure jump route against jump+gate, and show "
                            "where a short gate run replaces several jumps.")
        b_assist.clicked.connect(self.gate_assist_requested)
        act_row.addWidget(compressible(b_assist, 60))
        b_rev = QPushButton("Reverse")
        b_rev.clicked.connect(self.reverse)
        self.b_copy = b_copy = QPushButton("Copy/Paste")
        b_copy.setToolTip("Copy the route as text or as a Dotlan link, or "
                          "load a route from a Dotlan link.")
        b_rev.setToolTip("Fly the same waypoints in reverse.")
        b_copy.clicked.connect(self._copy_menu)
        act_row.addWidget(compressible(b_rev, 60))
        act_row.addWidget(compressible(b_copy, 60))
        v.addLayout(act_row)

        self.b_saved = QPushButton("Saved routes")
        self.b_saved.setToolTip(
            "Store the current waypoints under a name and load them back later.")
        self.b_saved.clicked.connect(self._saved_menu)
        v.addWidget(compressible(self.b_saved, 80))

        self._sync_sections()

    # ---- auto in-game waypoint ---------------------------------------------
    def auto_waypoint_target(self):
        """Where to send the in-game autopilot, and when to send it.

        Only "gate" legs are things the in-game autopilot actually flies --
        "jump", "bridge" and "hole" all need a manual action first (jump
        activation, portal, flying the hole). So the moment worth waiting for
        is landing on the far side of the LAST such manual leg, provided
        everything after it really is gate-only the rest of the way: that is
        exactly the point where there is nothing left to do but let the
        client's own autopilot carry on to the end.

        Returns None if there is no such moment -- fewer than two waypoints,
        no manual leg at all (already gates the whole way, so autopilot could
        have been set from the start), or a manual leg that is not followed
        by an unbroken run of gates (nothing to hand off to).

        Otherwise returns (trigger_system, dest_system, dest_location_id):
        dest_location_id is the chosen dock's own id (station_id or
        structure location_id -- the only one ESI's waypoint endpoint
        actually accepts) when the final waypoint has one, else 0 to fall
        back to the bare destination system.
        """
        modes = self.route_modes
        if len(self.waypoints) < 2 or not modes:
            return None
        last_manual = None
        for i, m in enumerate(modes):
            if m != "gate":
                last_manual = i
        if last_manual is None:
            return None                      # already gates the whole way
        if any(m != "gate" for m in modes[last_manual + 1:]):
            return None                      # a manual leg follows -- not a clean handoff
        trigger = self.waypoints[last_manual + 1].system
        dest_wp = self.waypoints[-1]
        if trigger.id == dest_wp.system.id:
            return None                      # jump lands you there directly;
                                             # no gate handoff to arm for
        dest_location_id = 0
        dock = effective_dock(dest_wp, self._docks(dest_wp.system.id))
        if dock is not None and dock.can_dock and dock.location_id:
            dest_location_id = dock.location_id
        return trigger, dest_wp.system, dest_location_id

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

    def use_wormholes(self) -> bool:
        """Whether the planner may route over scouted wormholes.

        Forced on under "Fastest", for every hull. A wormhole is a gate that
        happens to be temporary, and ignoring one can cost a dozen jumps --
        so asking for the fastest route and not being shown it is just a
        wrong answer. The hole's mass limit still decides what fits, which is
        the only thing that should depend on the ship.
        """
        return self.chk_holes.isChecked() or self.gate_pref() == "fast"

    def _sync_hole_toggle(self):
        """Show that Fastest has taken the choice out of the user's hands."""
        forced = self.gate_pref() == "fast"
        self.chk_holes.setEnabled(not forced)
        # Appending the reason to the label made it the widest thing in the
        # panel, which set the panel's minimum width and pushed Find off the
        # edge. The tooltip carries it instead.
        self.chk_holes.setToolTip(
            "Always on while Gates is set to Fastest."
            if forced else self._HOLES_TIP)

    def set_hole_status(self, total: int, passable: int, hull: str,
                        stale: bool = False):
        """Say what the scouted connections amount to for this hull.

        Worth its own line because the interesting case is silent otherwise:
        a dozen holes scouted and not one of them big enough, which would
        otherwise look identical to nothing having been fetched.
        """
        if not self.chk_holes.isChecked():
            self.lbl_holes.setVisible(False)
            return
        if not total:
            msg, colour = "No EVE-Scout connections available.", WARN
        elif not passable:
            msg = (f"{total} wormhole{'s' if total != 1 else ''} scouted, "
                   f"none passable by a {hull}.")
            colour = WARN
        else:
            msg = (f"{passable} of {total} scouted wormholes fit a {hull}.")
            colour = TEXT_MUTED
        if stale:
            msg += "  Connections may have expired — refresh before flying."
            colour = WARN
        self.lbl_holes.setText(msg)
        self.lbl_holes.setStyleSheet(f"color: {colour};")
        self.lbl_holes.setVisible(True)

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

    def clear_waypoints(self):
        """Public name for the same thing, for callers outside this panel."""
        self._clear()

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
        from ... import config
        from ..dialogs import SystemInfoDialog
        uni = self.ctx.universe
        if uni is None or system_id not in uni.systems:
            return
        s = uni.systems[system_id]
        before = config.get_system_notes().get(s.name, "")
        dlg = SystemInfoDialog(
            self, s,
            f"Region: {uni.region_names.get(s.region_id, str(s.region_id))}",
            self.ctx.sov_of(s.id),
            self.ctx.system_intel(s.id),
            cyno_cb=self.ctx.check_cyno_activity,
            note=before,
        )
        dlg.exec()
        if dlg.note() != before:
            config.set_system_note(s.name, dlg.note())
            self.ctx.refresh_notes()

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
            if leg.mode == "hole":
                # Which hub, and the signature to search for at this end --
                # without those the leg cannot actually be flown.
                info = (self.ctx.universe.hole_between(leg.src.id, leg.dst.id)
                        if getattr(self.ctx, "universe", None) else None)
                via = (info or {}).get("via", "wormhole")
                sig = (info or {}).get("sigs", {}).get(leg.src.id)
                label = f"{via.lower()} {sig}" if sig else via.lower()
                vals = [label, leg.src.name, leg.dst.name,
                        f"{leg.distance_ly:.2f}", "-", "-",
                        f"{leg.fatigue_after_min:.0f}m", "✓"]
            elif leg.mode == "bridge":
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
        """Offer matches in the popup instead of reflowing the panel."""
        uni = self.ctx.universe
        if not uni:
            return
        hits = list(uni.search(self.search.text()))
        if len(hits) == 1:
            self._add_system(hits[0].id)      # unambiguous: just add it
            return
        if not hits:
            self.totals.setText(f"No system matches {self.search.text()!r}.")
            return
        self._completer_model.setStringList(
            [f"{s.name}  ({s.security:.1f})" for s in hits])
        self.completer.complete()

    def _on_search_text(self, text: str):
        """Keep the popup's list current as the user types."""
        uni = self.ctx.universe
        if uni is None or len(text) < 2:
            return
        self._completer_model.setStringList(
            [f"{s.name}  ({s.security:.1f})" for s in uni.search(text)])

    def _on_completed(self, text: str):
        uni = self.ctx.universe
        if uni is None:
            return
        s = uni.by_name(text.split("  (")[0])
        if s is not None:
            self._add_system(s.id)
            self.search.clear()

    # ---- copy -------------------------------------------------------------
    def _copy_menu(self):
        """Share the route as text or as a Dotlan link, or read one back.

        A Dotlan link is worth more than any format of ours: it opens in a
        browser for people who do not run this app, which is most of a corp.
        """
        menu = QMenu(self)
        act_text = menu.addAction("Copy as text")
        act_text.setEnabled(bool(self.waypoints))
        act_link = menu.addAction("Copy Dotlan link")
        act_link.setEnabled(bool(self.waypoints))
        act_img = menu.addAction("Copy route image")
        act_img.setEnabled(bool(self.waypoints))
        menu.addSeparator()
        act_paste = menu.addAction("Paste Dotlan link…")

        chosen = menu.exec(self.b_copy.mapToGlobal(self.b_copy.rect().bottomLeft()))
        if chosen is act_text:
            self._copy_route()
        elif chosen is act_link:
            self._copy_dotlan()
        elif chosen is act_img:
            self._copy_image()
        elif chosen is act_paste:
            self._paste_dotlan()

    def _copy_image(self):
        """A picture of the route on the map, framed and captioned."""
        image = self.ctx.route_image()
        if image is None:
            self.totals.setText("Nothing to draw yet.")
            return
        QGuiApplication.clipboard().setPixmap(image)
        self.totals.setText(f"Copied a {image.width()}x{image.height()} route "
                            "image to the clipboard.")

    def _copy_dotlan(self):
        from ...data import dotlan

        url = dotlan.build_url(self.ctx.ship_name(), *self.ctx.jump_skills(),
                               [w.system.name for w in self.waypoints])
        QGuiApplication.clipboard().setText(url)
        self.totals.setText(f"Copied a Dotlan link for "
                            f"{len(self.waypoints)} system(s).")

    def _paste_dotlan(self):
        from ...data import dotlan

        clip = QGuiApplication.clipboard().text().strip()
        text, ok = QInputDialog.getText(
            self, "Paste Dotlan link",
            "Paste a Dotlan jump link:", QLineEdit.EchoMode.Normal,
            clip if "/jump/" in clip else "")
        if not ok:
            return
        route = dotlan.parse_url(text)
        if route is None:
            self.totals.setText("That is not a Dotlan jump link.")
            return
        self.dotlan_imported.emit(route)

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
    def _sync_sections(self):
        """Keep each collapsed header showing what is set inside it.

        This is what makes collapsing safe: you can still read that you are
        avoiding incursions without opening the section to check.
        """
        jumps, safety, dock = self._sections

        bits = [self.cmb_balance.currentText().split(" - ")[0]]
        if not self.chk_gates.isChecked():
            bits = ["jump drive only"]
        if self.chk_ansiblex.isChecked():
            bits.append("Ansiblex")
        if self.chk_holes.isChecked():
            bits.append("wormholes")
        jumps.set_summary(" · ".join(bits))

        on = [name for name, w in (
            ("incursions", self.chk_incursions),
            ("hostiles", self.chk_hostile),
            ("kills", self.chk_kills),
            ("fatigue", self.chk_reactivation),
        ) if w.isChecked()]
        safety.set_summary(f"avoiding {', '.join(on)}" if on else "no filters")

        if not self.pick_docks():
            dock.set_summary("passing through")
        else:
            dock.set_summary(self.cmb_policy.currentText().lower())

    def _emit_changed(self):
        self._sync_sections()
        self._sync_auto_waypoint_label()
        self.changed.emit()

    def _sync_auto_waypoint_label(self):
        """Say plainly whether this is armed and for what, rather than let a
        checkbox that quietly does nothing on the current route look the
        same as one that is about to write to the game client."""
        lbl = self.lbl_auto_waypoint
        if not self.chk_auto_waypoint.isChecked():
            lbl.setVisible(False)
            return
        target = self.auto_waypoint_target()
        lbl.setVisible(True)
        if target is None:
            lbl.setText("Not armed - this route has no jump/bridge/hole "
                        "followed by gates to hand off to.")
            return
        trigger, dest, dock_id = target
        via = " (chosen dock)" if dock_id else ""
        lbl.setText(f"Armed: on reaching {trigger.name}, sets in-game "
                    f"destination to {dest.name}{via}.")

    def state(self) -> dict:
        return {
            "policy": self.cmb_policy.currentIndex(),
            "allow_gates": self.chk_gates.isChecked(),
            "balance": self.cmb_balance.currentIndex(),
            "use_ansiblex": self.chk_ansiblex.isChecked(),
            "use_wormholes": self.chk_holes.isChecked(),
            "gate": self.cmb_gate.currentIndex(),
            "min_reactivation": self.chk_reactivation.isChecked(),
            "exclude_hostile": self.chk_hostile.isChecked(),
            "avoid_incursions": self.chk_incursions.isChecked(),
            "avoid_kills": self.chk_kills.isChecked(),
            "no_docks": self.chk_nodocks.isChecked(),
            "auto_ingame_waypoint": self.chk_auto_waypoint.isChecked(),
            # System names, the same durable choice the explicit "Saved
            # routes" feature already makes: a name survives an SDE refresh
            # and stays readable in config.json, an id does not. This is what
            # was missing before -- every option above was already
            # autosaved, the waypoints themselves never were, session
            # autosave or a tool call over the MCP bridge alike.
            "waypoints": [wp.system.name for wp in self.waypoints],
            "route_modes": list(self.route_modes),
        }

    def restore_waypoints(self, s: dict):
        """Rebuild the waypoint list on startup, once the map exists.

        Deliberately separate from restore(): that method runs during
        __init__, before self.ctx.universe exists, and a waypoint needs the
        universe to resolve a system name against. Call this once, after the
        first universe load -- not on a later "Reload map data", which must
        not stomp on a route someone is actively editing.
        """
        uni = self.ctx.universe
        names = s.get("waypoints") or []
        if not uni or not names:
            return
        found, missing = [], []
        for n in names:
            sys_ = uni.by_name(n)
            (found if sys_ else missing).append(sys_ or n)
        for sys_ in found:
            self._add_system(sys_.id)
        modes = s.get("route_modes") or []
        if len(modes) == max(0, len(self.waypoints) - 1):
            self.route_modes = list(modes)
            self._rebuild()
        if missing:
            self.ctx.statusBar().showMessage(
                f"Restored route: {len(found)} of {len(names)} waypoint(s) - "
                "unknown: " + ", ".join(str(m) for m in missing[:6]), 8000)

    def restore(self, s: dict):
        # Options only. "waypoints"/"route_modes" in the same dict are for
        # restore_waypoints() -- this runs during __init__, before
        # self.ctx.universe exists to resolve a system name against.
        widgets = (self.cmb_policy, self.chk_gates, self.cmb_balance, self.chk_ansiblex,
                   self.chk_holes, self.cmb_gate, self.chk_reactivation,
                   self.chk_hostile, self.chk_incursions)
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
        self.chk_holes.setChecked(bool(s.get("use_wormholes", False)))
        self._sync_hole_toggle()
        self.cmb_gate.setCurrentIndex(int(s.get("gate", 0)))
        self.chk_reactivation.setChecked(bool(s.get("min_reactivation", False)))
        self.chk_hostile.setChecked(bool(s.get("exclude_hostile", False)))
        self.chk_incursions.setChecked(bool(s.get("avoid_incursions", False)))
        self.chk_kills.setChecked(bool(s.get("avoid_kills", False)))
        self.chk_nodocks.setChecked(bool(s.get("no_docks", False)))
        self.chk_auto_waypoint.setChecked(bool(s.get("auto_ingame_waypoint", False)))
        for w in widgets:
            w.blockSignals(False)
