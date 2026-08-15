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

        # -- in-app chat ----------------------------------------------------
        box = QGroupBox("In-app chat")
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
        self.btn_copy.clicked.connect(self._copy_config)
        row.addWidget(self.btn_copy)
        self.lbl_copied = _muted("")
        row.addWidget(self.lbl_copied, 1)
        mv.addLayout(row)
        mv.addWidget(_muted(
            "Paste that into claude_desktop_config.json, then restart Claude "
            "Desktop. Every tool call the server serves is appended to "
            "mcp-audit.log beside your settings."))
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
        for w in (self.chk_writes, self.chk_private, self.btn_copy):
            w.setEnabled(on)

    def _copy_config(self):
        QGuiApplication.clipboard().setText(claude_desktop_snippet())
        self.lbl_copied.setText("Copied.")

    def save(self):
        config.set_ai_chat_enabled(self.chk_chat.isChecked())
        provider = self.cmb_provider.currentData()
        config.set_ai_provider(provider)
        # An env-var key is echoed into the field; don't write it to disk.
        import os
        env_name = providers.env_var(provider)
        typed = self.txt_key.text().strip()
        if typed != (os.environ.get(env_name) or ""):
            config.set_ai_key(provider, typed)
        config.set_ai_model(provider, self.cmb_model.currentText().strip())
        config.set_mcp_enabled(self.chk_mcp.isChecked())
        config.set_mcp_allow_writes(self.chk_writes.isChecked())
        config.set_mcp_allow_private(self.chk_private.isChecked())


def claude_desktop_snippet() -> str:
    """The mcpServers entry to paste into Claude Desktop's config."""
    if getattr(sys, "frozen", False) or globals().get("__compiled__"):
        exe = str(Path(sys.executable).resolve())
        entry = {"command": exe, "args": ["--mcp"]}
    else:
        entry = {"command": sys.executable,
                 "args": ["-m", "eve_strait", "--mcp"],
                 "cwd": str(Path(__file__).resolve().parents[3])}
    return json.dumps({"mcpServers": {"eve-strait": entry}}, indent=2)
