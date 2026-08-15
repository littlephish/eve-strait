"""Configuration for the assistant and the MCP server.

Deliberately one dialog for both, because they share the same decision: how
much of your EVE intelligence you are willing to send to a third party.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import Qt

from .theme import TEXT_MUTED
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..ai import providers


def _muted(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{TEXT_MUTED}; font-size:11px;")
    lbl.setWordWrap(True)
    return lbl


def _link(html: str) -> QLabel:
    lbl = QLabel(html)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    lbl.setOpenExternalLinks(True)
    lbl.setWordWrap(True)
    return lbl


class AiSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI assistant")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)

        lay.addWidget(_link(
            "<b>Everything the assistant reads is sent to the provider you "
            "choose.</b> System names, your notes, your route, and anything "
            "else it looks up. Treat that the way you would treat posting it "
            "in a public channel."))

        # -- in-app chat (disabled) ------------------------------------------
        # Fields kept rather than removed, so a config from before this was
        # disabled is still visible here -- but this made direct API calls to
        # Claude and OpenAI, which cost real per-token billing and pulled in
        # ~20 packages (anthropic, openai and everything under them) just to
        # place those calls. Claude and ChatGPT now only ever reach this app
        # through the MCP server below, which needs neither SDK and runs on
        # whatever subscription you already have -- no key, no billing.
        box = QGroupBox("In-app chat (disabled -- use MCP below instead)")
        box.setEnabled(False)
        box.setToolTip(
            "Direct API chat is disabled in this build to avoid the API SDK "
            "dependencies and their per-token billing. The MCP server below "
            "does the same job through Claude Desktop or ChatGPT Desktop's "
            "own subscription instead.")
        bv = QVBoxLayout(box)
        self.chk_chat = QCheckBox("Show the chat panel")
        self.chk_chat.setToolTip(
            "Independent of the key below. Turn this off to close the panel "
            "without losing a key you may want to switch back on later; the "
            "panel will not reappear on its own if a key happens to be set.")
        bv.addWidget(self.chk_chat)

        form_frame = QWidget()
        form = QFormLayout(form_frame)
        form.setContentsMargins(0, 0, 0, 0)
        bv.addWidget(form_frame)
        self.cmb_provider = QComboBox()
        for key, info in providers.PROVIDERS.items():
            self.cmb_provider.addItem(info["label"], key)
        self.cmb_provider.currentIndexChanged.connect(self._provider_changed)
        form.addRow("Provider:", self.cmb_provider)

        self.txt_key = QLineEdit()
        self.txt_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_key.setPlaceholderText("Paste an API key")
        self.txt_key.textChanged.connect(self._refresh_state)
        form.addRow("API key:", self.txt_key)

        self.cmb_model = QComboBox()
        self.cmb_model.setEditable(True)
        form.addRow("Model:", self.cmb_model)

        self.lbl_keys = _link("")
        form.addRow("", self.lbl_keys)
        form.addRow("", _muted(
            "A Claude Pro/Max or ChatGPT Plus/Pro subscription does NOT "
            "include API access; API usage is billed separately per token. "
            "The key can also come from an environment variable matching "
            "the provider (ANTHROPIC_API_KEY, OPENAI_API_KEY, "
            "OPENROUTER_API_KEY), which keeps it off disk. The chat panel "
            "stays hidden until both this is on and a key is set."))
        lay.addWidget(box)

        # -- MCP ------------------------------------------------------------
        mcp = QGroupBox("MCP server (drive Eve-Strait from Claude Desktop)")
        mv = QVBoxLayout(mcp)
        mv.addWidget(_muted(
            "Lets Claude Desktop use Eve-Strait's tools on your existing "
            "subscription, with no API key and no per-token billing. It runs "
            "as a child process over stdin/stdout and opens no network port, "
            "so nothing is reachable from outside this machine."))
        self.chk_mcp = QCheckBox("Enable the MCP server")
        self.chk_mcp.setToolTip(
            "Off by default. The server itself re-checks this and refuses to "
            "serve if it is off, so a stale Claude Desktop entry cannot "
            "quietly keep working.")
        self.chk_mcp.toggled.connect(self._refresh_state)
        mv.addWidget(self.chk_mcp)

        self.chk_writes = QCheckBox(
            "Allow it to change things (notes, avoid list)")
        self.chk_writes.setToolTip(
            "Off by default. Without this the server is strictly read-only.")
        mv.addWidget(self.chk_writes)

        self.chk_private = QCheckBox(
            "Allow it to read your characters, locations and standings")
        self.chk_private.setToolTip(
            "Off by default even when the server is on. This is the real "
            "intelligence leak: structure names, standings and where your "
            "characters are parked.")
        mv.addWidget(self.chk_private)

        row = QHBoxLayout()
        self.btn_copy = QPushButton("Copy Claude Desktop config")
        self.btn_copy.clicked.connect(self._copy_claude_config)
        row.addWidget(self.btn_copy)
        self.btn_copy_gpt = QPushButton("Copy ChatGPT / Codex config")
        self.btn_copy_gpt.clicked.connect(self._copy_chatgpt_config)
        row.addWidget(self.btn_copy_gpt)
        mv.addLayout(row)
        self.lbl_copied = _muted("")
        mv.addWidget(self.lbl_copied)
        mv.addWidget(_muted(
            "It is the same server either way, just a different client and a "
            "different config file. Claude Desktop reads JSON from "
            "claude_desktop_config.json; ChatGPT Desktop and Codex CLI share "
            "one TOML file at ~/.codex/config.toml (Settings → MCP "
            "servers in ChatGPT Desktop can add it directly instead). "
            "Restart whichever client after saving. Every tool call the "
            "server serves is appended to mcp-audit.log beside your "
            "settings, regardless of which client asked."))
        lay.addWidget(mcp)

        box_btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Cancel)
        box_btn.accepted.connect(self.accept)
        box_btn.rejected.connect(self.reject)
        lay.addWidget(box_btn)

        self._load()

    # ------------------------------------------------------------------
    def _load(self):
        self.chk_chat.setChecked(config.get_ai_chat_enabled())
        provider = config.get_ai_provider()
        i = self.cmb_provider.findData(provider)
        self.cmb_provider.setCurrentIndex(max(0, i))
        self._provider_changed()
        self.chk_mcp.setChecked(config.get_mcp_enabled())
        self.chk_writes.setChecked(config.get_mcp_allow_writes())
        self.chk_private.setChecked(config.get_mcp_allow_private())
        self._refresh_state()

    def _provider_changed(self):
        key = self.cmb_provider.currentData()
        info = providers.PROVIDERS[key]
        self.cmb_model.clear()
        self.cmb_model.addItems(info["models"])
        current = config.get_ai_model(key)
        self.cmb_model.setCurrentText(current or info["models"][0])
        self.txt_key.setText(config.get_ai_key(key))
        self.lbl_keys.setText(
            f'<a href="{info["keys_url"]}">Get a {info["label"]} API key</a>')

    def _refresh_state(self):
        on = self.chk_mcp.isChecked()
        for w in (self.chk_writes, self.chk_private,
                 self.btn_copy, self.btn_copy_gpt):
            w.setEnabled(on)

    def _copy_claude_config(self):
        QGuiApplication.clipboard().setText(claude_desktop_snippet())
        self.lbl_copied.setText("Copied the Claude Desktop (JSON) config.")

    def _copy_chatgpt_config(self):
        QGuiApplication.clipboard().setText(chatgpt_desktop_snippet())
        self.lbl_copied.setText(
            "Copied the ChatGPT Desktop / Codex CLI (TOML) config.")

    def save(self):
        # The in-app chat box above is disabled and not settable from here
        # any more -- nothing in it to write back. Whatever was saved before
        # this was disabled is left untouched on disk rather than rewritten
        # with the same (inert) values on every save.
        config.set_mcp_enabled(self.chk_mcp.isChecked())
        config.set_mcp_allow_writes(self.chk_writes.isChecked())
        config.set_mcp_allow_private(self.chk_private.isChecked())


def _mcp_command() -> tuple[str, list[str], str | None]:
    """(command, args, cwd) to launch this same install as --mcp.

    One function for both destinations, so a built exe vs. a dev checkout is
    decided in exactly one place rather than risking the two snippets
    disagreeing about which this install actually is.
    """
    if getattr(sys, "frozen", False) or globals().get("__compiled__"):
        return str(Path(sys.executable).resolve()), ["--mcp"], None
    return (sys.executable, ["-m", "eve_strait", "--mcp"],
            str(Path(__file__).resolve().parents[3]))


def claude_desktop_snippet() -> str:
    """The mcpServers entry to paste into Claude Desktop's config."""
    command, args, cwd = _mcp_command()
    entry = {"command": command, "args": args}
    if cwd:
        entry["cwd"] = cwd
    return json.dumps({"mcpServers": {"eve-strait": entry}}, indent=2)


def chatgpt_desktop_snippet() -> str:
    """The [mcp_servers.eve-strait] block for ChatGPT Desktop / Codex CLI.

    Same server, same --mcp launch, different client and a different config
    format: ChatGPT Desktop and Codex CLI share ~/.codex/config.toml (TOML,
    not JSON) rather than Claude Desktop's claude_desktop_config.json. Codex's
    own key for the working directory is `cwd`, same name as Claude's.
    """
    command, args, cwd = _mcp_command()
    lines = ["[mcp_servers.eve-strait]",
             f'command = "{_toml_str(command)}"',
             "args = [" + ", ".join(f'"{_toml_str(a)}"' for a in args) + "]"]
    if cwd:
        lines.append(f'cwd = "{_toml_str(cwd)}"')
    return "\n".join(lines)


def _toml_str(value: str) -> str:
    """Escape a value for a TOML basic string. Windows paths need this: a
    bare backslash in "C:\\eve-strait.exe" would otherwise read as an escape."""
    return value.replace("\\", "\\\\").replace('"', '\\"')
