"""Small popup dialogs (station info with render image, owner and standing)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)


def _link_label(html: str) -> QLabel:
    """A rich-text label whose hyperlinks open in the system browser."""
    lbl = QLabel(html)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    lbl.setOpenExternalLinks(True)
    lbl.setWordWrap(True)
    return lbl


class _CopyRow(QHBoxLayout):
    """Read-only value with a Copy button."""

    def __init__(self, value: str):
        super().__init__()
        field = QLineEdit(value)
        field.setReadOnly(True)
        btn = QPushButton("Copy")
        btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(value))
        self.addWidget(field, 1)
        self.addWidget(btn)


class EsiSetupDialog(QDialog):
    """Guided EVE application setup: callback URL, scopes and Client ID."""

    def __init__(self, parent, client_id: str, callback_url: str, scopes: list[str]):
        super().__init__(parent)
        self.setWindowTitle("EVE ESI setup")
        self.setMinimumWidth(620)
        v = QVBoxLayout(self)

        v.addWidget(_link_label(
            'Create an application at '
            '<a href="https://developers.eveonline.com">developers.eveonline.com</a> '
            '(<a href="https://developers.eveonline.com/applications/create">create one '
            'directly</a>), then paste its <b>Client ID</b> below.<br><br>'
            'Set <b>Connection Type</b> to <b>Authentication &amp; API Access</b> - '
            '"Authentication Only" makes every scope fail with '
            '<code>invalid_scope</code>.'))

        v.addWidget(QLabel("<b>Callback URL</b> - must match exactly:"))
        v.addLayout(_CopyRow(callback_url))

        v.addWidget(_link_label(
            f"<b>Scopes</b> - tick all {len(scopes)} when creating the application. "
            "Adding them later means logging in again. "
            '<a href="https://developers.eveonline.com/applications">Manage applications</a>'))
        box = QPlainTextEdit("\n".join(scopes))
        box.setReadOnly(True)
        box.setFixedHeight(150)
        v.addWidget(box)

        copy_row = QHBoxLayout()
        copy_row.addStretch(1)
        b_list = QPushButton("Copy scope list")
        b_list.clicked.connect(
            lambda: QGuiApplication.clipboard().setText("\n".join(scopes)))
        b_json = QPushButton("Copy as JSON")
        b_json.clicked.connect(lambda: QGuiApplication.clipboard().setText(
            "[" + ",".join(f'"{s}"' for s in scopes) + "]"))
        copy_row.addWidget(b_list)
        copy_row.addWidget(b_json)
        v.addLayout(copy_row)

        v.addWidget(QLabel("<b>Client ID</b>"))
        self.field = QLineEdit(client_id)
        self.field.setPlaceholderText("paste your application's Client ID here")
        v.addWidget(self.field)

        v.addWidget(_link_label(
            "<i>No secret key is needed - this app uses OAuth2 PKCE, so only the "
            "Client ID is stored.</i>"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def client_id(self) -> str:
        return self.field.text().strip()


def standing_html(standing, label: str = "") -> str:
    """Colored standing text: + dark blue, - dark red (EVE contact colors)."""
    suffix = f" <span style='color:#888'>({label})</span>" if label else ""
    if standing is None:
        return ("<span style='color:#888'>not in your character, corp or "
                "alliance contacts</span>")
    if standing > 0:
        return f"<b style='color:#1f3fb0'>+{standing:.1f}</b>{suffix}"
    if standing < 0:
        return f"<b style='color:#b01f1f'>{standing:.1f}</b>{suffix}"
    return f"<span style='color:#888'>0.0 (neutral)</span>{suffix}"


def _fmt_time(minutes: float) -> str:
    if minutes >= 60:
        return f"{minutes / 60:.1f} h"
    return f"{minutes:.0f} min"


def _adm_note(adm) -> str:
    """Plain reading of an ADM number.

    ADM rises with sustained ratting, mining and industry and decays without
    them, so it is the closest honest answer to "does anyone actually live
    here". It is not a player count; nothing in ESI is.
    """
    if adm is None:
        return ""
    if adm >= 5.5:
        return "heavily used, expect people on most of the day"
    if adm >= 4.0:
        return "regularly used"
    if adm >= 2.5:
        return "some activity"
    if adm > 1.0:
        return "barely used"
    return "dead, nothing is being done here"


class SystemInfoDialog(QDialog):
    """Everything known about one system, with the caveats attached.

    Split into what is measured (gate traffic, kills, ratting, ADM, indices)
    and what is inferred. ESI publishes no per-system player count, so the
    dialog never claims one: gate traffic and ADM are labelled as the proxies
    they are.
    """

    def __init__(self, parent, system, region: str, sov, intel: dict,
                 cyno_cb=None, note: str = ""):
        super().__init__(parent)
        self.setWindowTitle(f"System: {system.name}")
        self.setMinimumWidth(430)
        self._cyno_cb = cyno_cb
        self._system = system

        lay = QVBoxLayout(self)
        kind = ("high-sec" if system.security >= 0.5 else
                "low-sec" if system.security > 0.0 else "null-sec")
        sec_col = ("#2c9e4b" if system.security >= 0.5 else
                   "#d08b23" if system.security > 0.0 else "#b01f1f")
        lay.addWidget(_link_label(
            f"<div style='font-size:15px'><b>{system.name}</b> "
            f"<span style='color:{sec_col}'>({system.security:.2f}, {kind})</span>"
            f"</div>{region}"))

        # -- sovereignty ---------------------------------------------------
        rows = []
        if sov:
            owner, otype, standing, label = sov
            rows.append(("Sovereignty", f"<b>{owner}</b> ({otype})"))
            rows.append(("Standing", standing_html(standing, label)))
        elif system.security <= 0.0:
            rows.append(("Sovereignty", "<i>unclaimed</i>"))
        adm = intel.get("adm")
        if adm is not None:
            note = _adm_note(adm)
            rows.append(("Defense (ADM)",
                         f"<b>{adm:.2f}</b> of 6.00"
                         f" <span style='color:#888'>({note})</span>"))
            window = " to ".join(x[11:16] for x in
                                 (intel.get("vuln_start"), intel.get("vuln_end"))
                                 if x and len(x) >= 16)
            if window:
                rows.append(("Vulnerable", f"{window} EVE time"))
        rows.append(("Jump target",
                     "yes" if system.jumpable else "no (high-sec)"))
        lay.addLayout(self._grid(rows))

        # -- activity ------------------------------------------------------
        hours = intel.get("history_hours", 0)
        k24 = intel.get("kills_24h") or {}
        span = f"{hours}h" if hours < 24 else "24h"
        act = [
            ("Gate traffic", self._pair(intel.get("jumps_1h", 0),
                                        intel.get("jumps_24h", 0), span)),
            ("Ratting (NPC kills)", f"{intel.get('npc_kills_1h', 0):,} last hour"),
            ("Ship kills", self._pair(intel.get("ship_kills_1h", 0),
                                      k24.get("ship", 0), span)),
            ("Pod kills", self._pair(intel.get("pod_kills_1h", 0),
                                     k24.get("pod", 0), span)),
        ]
        ind = intel.get("industry") or {}
        if ind.get("manufacturing") is not None:
            act.append(("Industry index",
                        f"{ind['manufacturing'] * 100:.2f}% manufacturing"))
        lay.addWidget(_section("Activity"))
        lay.addLayout(self._grid(act))
        note = ("ESI reports kills and traffic for the last full hour only. "
                "The 24h column is accumulated while Eve-Strait is running, so "
                "it is partial until it has been open a day.")
        if hours < 24:
            note += f" Currently {hours}h of history."
        lay.addWidget(_muted(note))

        # -- cyno ----------------------------------------------------------
        lay.addWidget(_section("Cyno activity"))
        self.lbl_cyno = _link_label(
            "<i>Not checked.</i> Nothing in ESI reports cynos, so this is "
            "built from killmails: ships that died with a cyno fitted.")
        lay.addWidget(self.lbl_cyno)
        self.btn_cyno = QPushButton("Check killmails for cyno losses")
        self.btn_cyno.clicked.connect(self._check_cyno)
        self.btn_cyno.setEnabled(cyno_cb is not None)
        lay.addWidget(self.btn_cyno)

        # -- notes ---------------------------------------------------------
        lay.addWidget(_section("Your notes"))
        self.txt_note = QPlainTextEdit(note)
        self.txt_note.setPlaceholderText(
            "Gate camp on the Amarr side. Friendly Fortizar. Cyno alt parked "
            "here.")
        self.txt_note.setFixedHeight(64)
        lay.addWidget(self.txt_note)
        lay.addWidget(_muted("Saved when you close this dialog. Clear the box "
                             "to delete the note."))

        dotlan = system.name.replace(" ", "_")
        lay.addWidget(_link_label(
            f'<a href="https://evemaps.dotlan.net/system/{dotlan}">Dotlan</a> '
            f'&nbsp;|&nbsp; '
            f'<a href="https://zkillboard.com/system/{system.id}/">zKillboard</a>'))

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.accept)   # closing still commits the note
        lay.addWidget(box)

    def note(self) -> str:
        return self.txt_note.toPlainText().strip()

    @staticmethod
    def _pair(now, day, span: str) -> str:
        # With only one hour of history the accumulated column just repeats
        # the live one, so leave it out rather than show the same number twice.
        if span == "1h":
            return f"<b>{now:,}</b> last hour"
        return f"<b>{now:,}</b> last hour &nbsp;/&nbsp; {day:,} in {span}"

    @staticmethod
    def _grid(rows):
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        for r, (name, value) in enumerate(rows):
            lbl = QLabel(f"{name}:")
            lbl.setStyleSheet("color:#888")
            grid.addWidget(lbl, r, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(_link_label(value), r, 1)
        return grid

    def _check_cyno(self):
        self.btn_cyno.setEnabled(False)
        self.lbl_cyno.setText("Querying zKillboard...")
        self._cyno_cb(self._system.id, self._on_cyno)

    def _on_cyno(self, text: str):
        self.lbl_cyno.setText(text)
        self.btn_cyno.setEnabled(True)


def _section(title: str) -> QLabel:
    lbl = QLabel(title)
    lbl.setStyleSheet("font-weight:bold; margin-top:8px;")
    return lbl


def _muted(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet("color:#888; font-size:11px;")
    lbl.setWordWrap(True)
    return lbl


class GateAssistDialog(QDialog):
    """Shows what stargate hops buy you versus jumping the whole way."""

    def __init__(self, parent, origin_name: str, dest_name: str, analysis: dict):
        super().__init__(parent)
        self.setWindowTitle("Gate assist")
        self.setMinimumWidth(600)
        v = QVBoxLayout(self)
        v.addWidget(_link_label(
            f"<b>{origin_name} → {dest_name}</b>"))

        jump_only = analysis.get("jump_only")
        mixed = analysis.get("mixed")
        gating = analysis.get("gating")

        rows = []
        for title, plan in (("Jumps only", jump_only),
                            ("Prefer jumping (gates allowed)", mixed),
                            ("Prefer gating", gating)):
            if not plan:
                rows.append(f"<tr><td>{title}</td><td colspan=5>"
                            "<i>not possible</i></td></tr>")
                continue
            rows.append(
                "<tr>"
                f"<td>{title}</td>"
                f"<td align=right>{plan['jumps']}</td>"
                f"<td align=right>{plan['gates']}</td>"
                f"<td align=right>{plan['fuel']:,}</td>"
                f"<td align=right>{_fmt_time(plan['time_min'])}</td>"
                f"<td align=right>{plan['peak_fatigue']:.0f} min</td>"
                "</tr>")

        table = QLabel(
            "<table cellpadding=5 width=100%>"
            "<tr><th align=left>Route</th><th align=right>Jumps</th>"
            "<th align=right>Gates</th><th align=right>Fuel</th>"
            "<th align=right>Time</th><th align=right>Peak fatigue</th></tr>"
            + "".join(rows) + "</table>")
        table.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(table)

        v.addWidget(QLabel(self._verdict(analysis)))

        runs = analysis.get("runs") or []
        if runs:
            items = []
            for r in runs:
                span = (f" spanning <b>{r['span_ly']:.1f} ly</b>"
                        if r["span_ly"] >= 1.0 else "")
                flag = ("  <b style='color:#b01f1f'>- mandatory: no jump route "
                        "avoids this</b>" if r["mandatory"] else "")
                items.append(
                    f"<li><b>{r['from'].name} → {r['to'].name}</b> - "
                    f"{r['hops']} gate hop{'s' if r['hops'] > 1 else ''}"
                    f"{span}{flag}</li>")
            gl = QLabel("<b>Gate sections of the best route</b><ul>"
                        + "".join(items) + "</ul>")
            gl.setTextFormat(Qt.TextFormat.RichText)
            gl.setWordWrap(True)
            v.addWidget(gl)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        v.addWidget(buttons)

    @staticmethod
    def _verdict(analysis: dict) -> str:
        mixed = analysis.get("mixed")
        if not mixed:
            return "No route found under the current filters."
        saved = analysis.get("saved")
        if saved is None:
            return ("⚑ Gates are the only way through - a pure jump route is "
                    "impossible (high-sec origin or destination).")
        if saved["jumps"] > 0:
            return (f"⚑ Gating saves {saved['jumps']} jump(s), "
                    f"{saved['fuel']:,} isotopes and "
                    f"{saved['fatigue']:.0f} min of peak fatigue "
                    f"for {mixed['gates']} gate hop(s).")
        return "Jumping the whole way is already optimal here."


class AnsiblexDialog(QDialog):
    """Manage Ansiblex jump-gate links (one pair per line)."""

    def __init__(self, parent, pairs: list[list[str]]):
        super().__init__(parent)
        self.setWindowTitle("Ansiblex jump gates")
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        v.addWidget(_link_label(
            "One link per line, as <code>SystemA &lt;-&gt; SystemB</code> "
            "(or <code>A » B</code>, the in-game gate name format).<br>"
            "Example: <code>HB-5L3 &lt;-&gt; SF-XJS</code><br><br>"
            "Links are treated as usable in both directions, cost one "
            "activation regardless of distance, burn no ship fuel - but still "
            "apply jump fatigue and a reactivation timer.<br><br>"
            "<b>Load from ESI</b> pulls your corporation's gates and adopts any "
            "owned by <b>your corp or alliance</b> that turn up while browsing "
            "systems. Gates owned by anyone else are ignored, since only the "
            "owning alliance can use them. Lines you type here are always kept."))
        self.box = QPlainTextEdit("\n".join(f"{a} <-> {b}" for a, b in pairs))
        self.box.setPlaceholderText("HB-5L3 <-> SF-XJS")
        self.box.setFixedHeight(180)
        v.addWidget(self.box)

        find_row = QHBoxLayout()
        find_row.addWidget(QLabel("Find gates in system:"))
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("1DQ1-A")
        find_row.addWidget(self.search_field, 1)
        self.btn_search = QPushButton("Search")
        self.btn_search.setToolTip(
            "Look up Ansiblex gates in one system using ESI structure search. "
            "Works with only the esi-search scope - no corp role needed.")
        find_row.addWidget(self.btn_search)
        v.addLayout(find_row)

        load_row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        load_row.addWidget(self.status, 1)
        self.btn_esi = QPushButton("Load from ESI")
        self.btn_esi.setToolTip(
            "Discover your corporation's Ansiblex gates via ESI. Each gate is "
            "named \"A » B\", so its name gives the whole link.")
        load_row.addWidget(self.btn_esi)
        v.addLayout(load_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def merge_links(self, links: list[list[str]], errors: list[str] | None = None):
        """Add ESI-discovered links, keeping what the user already typed."""
        existing = {tuple(sorted(p)) for p in self.pairs()}
        added = 0
        for a, b in links:
            if tuple(sorted((a, b))) in existing:
                continue
            existing.add(tuple(sorted((a, b))))
            self.box.appendPlainText(f"{a} <-> {b}")
            added += 1
        msg = f"Added {added} link(s) from ESI."
        if errors:
            msg += "  " + errors[0]
        self.status.setText(msg)

    def pairs(self) -> list[list[str]]:
        out = []
        for line in self.box.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            for sep in ("<->", "»", "<>", "->", "|", ","):
                if sep in line:
                    a, _, b = line.partition(sep)
                    if a.strip() and b.strip():
                        out.append([a.strip(), b.strip()])
                    break
        return out


class UpdateDialog(QDialog):
    """Offers a newer release: install it, or just open the release page."""

    def __init__(self, parent, info: dict, current: str, can_install: bool):
        super().__init__(parent)
        self.setWindowTitle("Update available")
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)

        size = f" ({info['zip_size'] / 1048576:.0f} MB)" if info.get("zip_size") else ""
        v.addWidget(_link_label(
            f"<b>Eve-Strait {info['version']}</b> is available "
            f"(you have {current}).{size}<br>"
            f'<a href="{info.get("page", "")}">View the release notes</a>'))

        notes = (info.get("notes") or "").strip()
        if notes:
            box = QPlainTextEdit(notes[:4000])
            box.setReadOnly(True)
            box.setFixedHeight(160)
            v.addWidget(box)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_install = QPushButton("Download and install")
        self.btn_later = QPushButton("Later")
        self.btn_later.clicked.connect(self.reject)
        if not can_install:
            self.btn_install.setEnabled(False)
            self.btn_install.setToolTip(
                "In-place updates are only available in the packaged build. "
                "Running from source? Use git pull.")
        row.addWidget(self.btn_install)
        row.addWidget(self.btn_later)
        v.addLayout(row)

    def set_busy(self, text: str):
        self.status.setText(text)
        self.btn_install.setEnabled(False)


class DockingRightsDialog(QDialog):
    """Corps / alliances whose structures you may dock at, whatever the standing."""

    def __init__(self, parent, names: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Docking rights")
        self.setMinimumWidth(480)
        v = QVBoxLayout(self)
        v.addWidget(_link_label(
            "One <b>corporation or alliance name</b> per line. Their structures "
            "are treated as usable for docking even when the entity is neutral "
            "or red, which is the normal case for rentals, NAPs and access "
            "deals.<br><br>"
            "These rank <b>above</b> merely positive standings when picking a "
            "dock, and are never dropped by <i>Exclude hostile-owned "
            "structures</i>. Names are resolved through ESI, so spell them as "
            "they appear in game."))
        self.box = QPlainTextEdit("\n".join(names))
        self.box.setPlaceholderText("Some Rental Alliance\nFriendly Holding Corp")
        self.box.setFixedHeight(180)
        v.addWidget(self.box)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def names(self) -> list[str]:
        return [n.strip() for n in self.box.toPlainText().splitlines() if n.strip()]


class IntelSettingsDialog(QDialog):
    """How often to re-poll kill, traffic, ADM and industry data."""

    CHOICES = [("Never (manual only)", 0), ("Every 15 minutes", 15),
               ("Every 30 minutes", 30), ("Every hour (recommended)", 60),
               ("Every 3 hours", 180)]

    def __init__(self, parent, current_minutes: int):
        super().__init__(parent)
        self.setWindowTitle("Intel refresh")
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        v.addWidget(_link_label(
            "Kill activity, gate traffic, sovereignty defense (ADM) and "
            "industry indices are re-polled on this interval.<br><br>"
            "ESI caches these for an hour, so polling faster returns the same "
            "numbers. The 24 hour totals are built from the samples taken while "
            "the app is running, so a longer interval means thinner history."))

        self.cmb = QComboBox()
        for label, minutes in self.CHOICES:
            self.cmb.addItem(label, minutes)
        idx = self.cmb.findData(current_minutes)
        self.cmb.setCurrentIndex(idx if idx >= 0 else 3)
        v.addWidget(self.cmb)

        self.chk_now = QCheckBox("Refresh now")
        self.chk_now.setChecked(True)
        v.addWidget(self.chk_now)

        v.addWidget(_link_label(
            "<br><b>Historical intel</b><br>"
            "Keep every sample on disk so system activity can be compared over "
            "time. Off by default: this grows by roughly a million rows a week, "
            "stored in a SQLite database next to the map cache."))

        self.cmb_history = QComboBox()
        for label, days in (("Don't store history", 0), ("Keep 7 days", 7),
                            ("Keep 30 days", 30), ("Keep 90 days", 90),
                            ("Keep 1 year", 365)):
            self.cmb_history.addItem(label, days)
        v.addWidget(self.cmb_history)

        self.lbl_stats = QLabel("")
        self.lbl_stats.setWordWrap(True)
        self.lbl_stats.setStyleSheet("color:#888")
        v.addWidget(self.lbl_stats)

        self.btn_purge = QPushButton("Delete stored history")
        v.addWidget(self.btn_purge)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def set_history_days(self, days: int):
        idx = self.cmb_history.findData(days)
        self.cmb_history.setCurrentIndex(idx if idx >= 0 else 0)

    def history_days(self) -> int:
        return int(self.cmb_history.currentData() or 0)

    def set_stats(self, stats: dict):
        if not stats.get("rows"):
            self.lbl_stats.setText("No history stored yet.")
            return
        import datetime
        span = ""
        if stats.get("oldest") and stats.get("newest"):
            fmt = "%Y-%m-%d %H:%M"
            span = (" from "
                    + datetime.datetime.fromtimestamp(stats["oldest"]).strftime(fmt)
                    + " to "
                    + datetime.datetime.fromtimestamp(stats["newest"]).strftime(fmt))
        self.lbl_stats.setText(
            f"{stats['rows']:,} samples across {stats['systems']:,} systems"
            f"{span} ({stats['size_mb']:.1f} MB).")

    def minutes(self) -> int:
        return int(self.cmb.currentData() or 0)

    def refresh_now(self) -> bool:
        return self.chk_now.isChecked()


class AvoidDialog(QDialog):
    """Systems the router must never pass through (one name per line)."""

    def __init__(self, parent, names: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Avoided systems")
        self.setMinimumWidth(420)
        v = QVBoxLayout(self)
        v.addWidget(_link_label(
            "One system name per line. Routes will never pass through these "
            "systems - jumps, gates and Ansiblex alike.<br>"
            "<i>Tip: you can also right-click a system on the map and choose "
            "<b>Avoid this system</b>.</i>"))
        self.box = QPlainTextEdit("\n".join(names))
        self.box.setPlaceholderText("Rancer\nTama")
        self.box.setFixedHeight(200)
        v.addWidget(self.box)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def names(self) -> list[str]:
        return [n.strip() for n in self.box.toPlainText().splitlines() if n.strip()]


class StationInfoDialog(QDialog):
    def __init__(self, parent, system_name: str, dock, standing=None):
        super().__init__(parent)
        self.setWindowTitle("Station info")
        v = QVBoxLayout(self)

        title = QLabel(f"<b>{dock.name}</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(title)
        v.addWidget(QLabel(f"System: {system_name}"))
        v.addWidget(QLabel(f"Type: {dock.kind}"))

        status = "OK" if dock.can_dock else "no docking"
        if dock.can_dock and not dock.safe:
            status = "RISKY"
        if getattr(dock, "has_rights", False):
            status = "OK (docking rights)"
        v.addWidget(QLabel(f"Docking: {status} - {dock.note}"))

        if dock.kind == "structure":
            self.owner = QLabel("Owner: resolving…" if dock.owner_id else "Owner: -")
            v.addWidget(self.owner)
            self.alliance = QLabel("Alliance: resolving…" if dock.owner_id else "Alliance: -")
            v.addWidget(self.alliance)
            self.standing = QLabel(f"Standing: {standing_html(standing)}")
            self.standing.setTextFormat(Qt.TextFormat.RichText)
            v.addWidget(self.standing)
        else:
            self.owner = self.alliance = self.standing = None

        self.img = QLabel("loading image…")
        self.img.setFixedSize(256, 256)
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.img)

    def set_owner_details(self, details: dict, standing, label: str = ""):
        """Fill in owner corp, alliance and standing once ESI resolves them."""
        if self.owner is None:
            return
        details = details or {}
        self.owner.setText(f"Owner: {details.get('name') or '-'}")
        alliance = details.get("alliance_name") or (
            str(details["alliance_id"]) if details.get("alliance_id") else "")
        self.alliance.setText(f"Alliance: {alliance or '- (not in an alliance)'}")
        self.standing.setText(f"Standing: {standing_html(standing, label)}")

    def set_image(self, data: bytes | None):
        if not data:
            self.img.setText("(no image)")
            return
        pix = QPixmap()
        if pix.loadFromData(data):
            self.img.setPixmap(pix.scaledToWidth(
                256, Qt.TransformationMode.SmoothTransformation))
        else:
            self.img.setText("(no image)")
