"""Small popup dialogs (station info with render image, owner and standing)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout


def standing_html(standing) -> str:
    """Colored standing text: + dark blue, - dark red (EVE contact colors)."""
    if standing is None:
        return "<span style='color:#888'>no contact</span>"
    if standing > 0:
        return f"<b style='color:#1f3fb0'>+{standing:.1f}</b>"
    if standing < 0:
        return f"<b style='color:#b01f1f'>{standing:.1f}</b>"
    return "<span style='color:#888'>0.0 (neutral)</span>"


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
        v.addWidget(QLabel(f"Docking: {status} — {dock.note}"))

        if dock.kind == "structure":
            self.owner = QLabel(f"Owner: {dock.owner_id or '—'}")
            v.addWidget(self.owner)
            st = QLabel(f"Standing: {standing_html(standing)}")
            st.setTextFormat(Qt.TextFormat.RichText)
            v.addWidget(st)
        else:
            self.owner = None

        self.img = QLabel("loading image…")
        self.img.setFixedSize(256, 256)
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.img)

    def set_owner_name(self, name: str | None):
        if self.owner is not None and name:
            self.owner.setText(f"Owner: {name}")

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
