"""Ship selection and jump-skill inputs."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QWidget,
)
from PySide6.QtCore import Qt, Signal

from ..theme import pad
from ...data.ships import SHIPS, SHIPS_BY_NAME, Ship, Skills
from ...jump import mechanics


class ShipSkillsPanel(QWidget):
    changed = Signal()

    def __init__(self):
        super().__init__()
        form = pad(QFormLayout(self))
        # "Jump Freighters (JF only)" against a narrow dock would otherwise
        # squeeze the spin boxes to nothing; wrap the row instead.
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.ship_combo = QComboBox()
        for s in SHIPS:
            self.ship_combo.addItem(f"{s.name}  ·  {s.hull_class}", s.name)
        self.ship_combo.currentIndexChanged.connect(self._on_change)
        form.addRow("Ship", self.ship_combo)

        self.sp_jdc = self._spin(4)
        self.sp_jdo = self._spin(5)
        self.sp_jfc = self._spin(4)
        self.sp_jf = self._spin(4)
        form.addRow("Jump Drive Calibration", self.sp_jdc)
        form.addRow("Jump Drive Operation", self.sp_jdo)
        form.addRow("Jump Fuel Conservation", self.sp_jfc)
        form.addRow("Jump Freighters (JF only)", self.sp_jf)

        self.sp_fatigue = QDoubleSpinBox()
        self.sp_fatigue.setRange(0, 100)
        self.sp_fatigue.setSuffix(" %")
        self.sp_fatigue.valueChanged.connect(self._on_change)
        form.addRow("Fatigue reduction (implants)", self.sp_fatigue)

        self.chk_bridge = QCheckBox("Plan reach as bridge/portal")
        self.chk_bridge.toggled.connect(self._on_change)
        form.addRow(self.chk_bridge)

        self.lbl_range = QLabel("-")
        self.lbl_fuel = QLabel("-")
        self.lbl_iso = QLabel("-")
        self.lbl_bridge = QLabel("-")
        for lbl in (self.lbl_range, self.lbl_fuel, self.lbl_iso, self.lbl_bridge):
            lbl.setStyleSheet("font-weight:bold;color:#2a7de1")
        form.addRow("Max jump range", self.lbl_range)
        form.addRow("Fuel / light year", self.lbl_fuel)
        form.addRow("Isotope", self.lbl_iso)
        form.addRow("Bridge range", self.lbl_bridge)

        self._refresh_readouts()

    def _spin(self, default: int) -> QSpinBox:
        sp = QSpinBox()
        sp.setRange(0, 5)
        sp.setValue(default)
        sp.valueChanged.connect(self._on_change)
        return sp

    def _on_change(self, *_):
        self._refresh_readouts()
        self.changed.emit()

    # -- queries ------------------------------------------------------------
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

    def use_bridge(self) -> bool:
        return self.chk_bridge.isChecked()

    def reach_range(self) -> float:
        ship, skills = self.current_ship(), self.current_skills()
        if self.use_bridge() and ship.bridge_range_ly:
            return ship.bridge_range_ly
        return mechanics.max_range_ly(ship, skills)

    def _refresh_readouts(self):
        ship = self.current_ship()
        skills = self.current_skills()
        self.lbl_range.setText(f"{mechanics.max_range_ly(ship, skills):.2f} ly")
        self.lbl_fuel.setText(f"{mechanics.fuel_per_ly(ship, skills):,.0f} iso")
        self.lbl_iso.setText(ship.isotope)
        self.lbl_bridge.setText(
            f"{ship.bridge_range_ly:.1f} ly" if ship.bridge_range_ly else "-")
        self.chk_bridge.setEnabled(bool(ship.bridge_range_ly))

    # -- persistence --------------------------------------------------------
    def state(self) -> dict:
        return {
            "ship": self.ship_combo.currentData(),
            "jdc": self.sp_jdc.value(),
            "jdo": self.sp_jdo.value(),
            "jfc": self.sp_jfc.value(),
            "jf": self.sp_jf.value(),
            "fatigue_pct": self.sp_fatigue.value(),
            "bridge": self.chk_bridge.isChecked(),
        }

    def restore(self, s: dict):
        for w in (self.ship_combo, self.sp_jdc, self.sp_jdo, self.sp_jfc,
                  self.sp_jf, self.sp_fatigue, self.chk_bridge):
            w.blockSignals(True)
        if s.get("ship"):
            i = self.ship_combo.findData(s["ship"])
            if i >= 0:
                self.ship_combo.setCurrentIndex(i)
        self.sp_jdc.setValue(int(s.get("jdc", 4)))
        self.sp_jdo.setValue(int(s.get("jdo", 5)))
        self.sp_jfc.setValue(int(s.get("jfc", 4)))
        self.sp_jf.setValue(int(s.get("jf", 4)))
        self.sp_fatigue.setValue(float(s.get("fatigue_pct", 0)))
        self.chk_bridge.setChecked(bool(s.get("bridge", False)))
        for w in (self.ship_combo, self.sp_jdc, self.sp_jdo, self.sp_jfc,
                  self.sp_jf, self.sp_fatigue, self.chk_bridge):
            w.blockSignals(False)
        self._refresh_readouts()
