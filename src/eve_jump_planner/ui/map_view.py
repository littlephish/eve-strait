"""2D pan/zoom map of New Eden with region labels, range and route overlays."""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from ..data.universe import System, Universe

_IGNORE_XF = QGraphicsEllipseItem.GraphicsItemFlag.ItemIgnoresTransformations


def _sec_color(sec: float) -> QColor:
    if sec >= 0.5:
        return QColor("#4c8f3f")   # high-sec green
    if sec > 0.0:
        return QColor("#c9a227")   # low-sec yellow
    return QColor("#a33a3a")       # null-sec red


class MapView(QGraphicsView):
    system_clicked = Signal(int)   # left-click near a system
    system_context = Signal(int)   # right-click near a system (open menu)

    def __init__(self, universe: Universe):
        super().__init__()
        self.universe = universe
        self.scene_obj = QGraphicsScene(self)
        self.setScene(self.scene_obj)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QColor("#0b0e14"))
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        self._pos: dict[int, QPointF] = {}
        self._overlay: list = []
        self._labels: list = []
        self._panning = False
        self._pan_start = QPointF()
        self._moved = False
        self._hover_id = None

        self._build()
        self._build_hover()

    def _build_hover(self):
        self._hover_ring = QGraphicsEllipseItem(-7, -7, 14, 14)
        pen = QPen(QColor("#cfe3ff"), 1.6)
        self._hover_ring.setPen(pen)
        self._hover_ring.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self._hover_ring.setFlag(_IGNORE_XF, True)
        self._hover_ring.setZValue(7)
        self._hover_ring.hide()
        self.scene_obj.addItem(self._hover_ring)

        self._hover_text = QGraphicsSimpleTextItem()
        self._hover_text.setBrush(QBrush(QColor("#eaf2ff")))
        f = QFont()
        f.setPointSize(10)
        f.setBold(True)
        self._hover_text.setFont(f)
        self._hover_text.setFlag(_IGNORE_XF, True)
        self._hover_text.setZValue(8)
        self._hover_text.hide()
        self.scene_obj.addItem(self._hover_text)

    # -- build --------------------------------------------------------------
    def _build(self):
        # Region labels (behind everything).
        for name, x, z in self.universe.regions:
            t = QGraphicsSimpleTextItem(name)
            t.setBrush(QBrush(QColor(120, 140, 180, 140)))
            f = QFont()
            f.setPointSize(11)
            f.setBold(True)
            t.setFont(f)
            t.setPos(x, -z)
            t.setFlag(_IGNORE_XF, True)
            t.setZValue(-1)
            self.scene_obj.addItem(t)

        for s in self.universe.systems.values():
            x, y = s.x, -s.z  # north up
            self._pos[s.id] = QPointF(x, y)
            dot = QGraphicsEllipseItem(-2.0, -2.0, 4.0, 4.0)
            dot.setPos(x, y)
            dot.setBrush(QBrush(_sec_color(s.security)))
            dot.setPen(QPen(Qt.PenStyle.NoPen))
            dot.setFlag(_IGNORE_XF, True)
            dot.setToolTip(f"{s.name}  ({s.security:.1f})")
            dot.setZValue(1)
            self.scene_obj.addItem(dot)
        rect = self.scene_obj.itemsBoundingRect()
        self.scene_obj.setSceneRect(rect.adjusted(-20, -20, 20, 20))
        self.resetTransform()
        self.scale(6.0, 6.0)
        self.centerOn(rect.center())

    # -- overlays -----------------------------------------------------------
    def clear_overlays(self):
        for item in self._overlay + self._labels:
            self.scene_obj.removeItem(item)
        self._overlay.clear()
        self._labels.clear()

    def _add_marker(self, sys: System, color: str, radius: float, label: str | None):
        p = self._pos[sys.id]
        m = QGraphicsEllipseItem(-radius, -radius, 2 * radius, 2 * radius)
        m.setPos(p)
        m.setPen(QPen(QColor(color), 2.0))
        m.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        m.setFlag(_IGNORE_XF, True)
        m.setZValue(5)
        self.scene_obj.addItem(m)
        self._overlay.append(m)
        if label:
            t = QGraphicsSimpleTextItem(label)
            t.setBrush(QBrush(QColor(color)))
            f = QFont()
            f.setPointSize(9)
            t.setFont(f)
            t.setPos(p.x() + 6, p.y() - 6)
            t.setFlag(_IGNORE_XF, True)
            t.setZValue(6)
            self.scene_obj.addItem(t)
            self._labels.append(t)

    def draw_range_circle(self, origin: System, range_ly: float):
        p = self._pos[origin.id]
        c = QGraphicsEllipseItem(p.x() - range_ly, p.y() - range_ly,
                                 2 * range_ly, 2 * range_ly)
        pen = QPen(QColor("#3a7bd5"), 0.0)
        pen.setCosmetic(True)
        c.setPen(pen)
        c.setBrush(QBrush(QColor(58, 123, 213, 30)))
        c.setZValue(0)
        self.scene_obj.addItem(c)
        self._overlay.append(c)

    def draw_route(self, waypoints: list[System], modes: list[str], in_range: list[bool]):
        for i, (a, b) in enumerate(zip(waypoints, waypoints[1:])):
            pa, pb = self._pos[a.id], self._pos[b.id]
            line = QGraphicsLineItem(pa.x(), pa.y(), pb.x(), pb.y())
            mode = modes[i] if i < len(modes) else "jump"
            if mode == "gate":
                pen = QPen(QColor("#7fb2ff"), 1.2)
                pen.setStyle(Qt.PenStyle.DotLine)
            elif in_range[i]:
                pen = QPen(QColor("#e0e0e0"), 1.6)
            else:
                pen = QPen(QColor("#ff5555"), 1.6)
                pen.setStyle(Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            line.setPen(pen)
            line.setZValue(3)
            self.scene_obj.addItem(line)
            self._overlay.append(line)

    def show_plan(self, origin: System | None, waypoints: list[System],
                  modes: list[str], in_range: list[bool], range_ly: float,
                  reachable: list[System]):
        self.clear_overlays()
        if origin and range_ly > 0:
            self.draw_range_circle(origin, range_ly)
            for s in reachable[:4000]:
                d = QGraphicsEllipseItem(-1.5, -1.5, 3.0, 3.0)
                d.setPos(self._pos[s.id])
                d.setBrush(QBrush(QColor("#5bc0eb")))
                d.setPen(QPen(Qt.PenStyle.NoPen))
                d.setFlag(_IGNORE_XF, True)
                d.setZValue(2)
                self.scene_obj.addItem(d)
                self._overlay.append(d)
        if len(waypoints) >= 2:
            self.draw_route(waypoints, modes, in_range)
        for i, s in enumerate(waypoints):
            role = "#39ff14" if i == 0 else "#ffd23f"
            self._add_marker(s, role, 6.0, f"{i}: {s.name}" if i else s.name)

    # -- interaction --------------------------------------------------------
    def wheelEvent(self, event):
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        old = self.mapToScene(event.position().toPoint())
        self.scale(factor, factor)
        new = self.mapToScene(event.position().toPoint())
        delta = new - old
        self.translate(delta.x(), delta.y())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._panning = True
            self._moved = False
            self._pan_start = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.position() - self._pan_start
            if delta.manhattanLength() > 3:
                self._moved = True
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y()))
        else:
            self._update_hover(event.position())
        super().mouseMoveEvent(event)

    def _update_hover(self, view_pos):
        sid = self._nearest(view_pos)
        if sid == self._hover_id:
            return
        self._hover_id = sid
        if sid is None:
            self._hover_ring.hide()
            self._hover_text.hide()
            return
        p = self._pos[sid]
        self._hover_ring.setPos(p)
        self._hover_ring.show()
        self._hover_text.setText(self.universe.systems[sid].name)
        self._hover_text.setPos(p.x() + 9, p.y() + 4)
        self._hover_text.show()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            if not self._moved:
                sid = self._nearest(event.position())
                if sid is not None:
                    self.system_clicked.emit(sid)
        elif event.button() == Qt.MouseButton.RightButton:
            sid = self._nearest(event.position())
            if sid is not None:
                self.system_context.emit(sid)
        super().mouseReleaseEvent(event)

    def _nearest(self, view_pos) -> int | None:
        scene_pt = self.mapToScene(view_pos.toPoint())
        best_id, best_d2 = None, None
        for sid, p in self._pos.items():
            d2 = (p.x() - scene_pt.x()) ** 2 + (p.y() - scene_pt.y()) ** 2
            if best_d2 is None or d2 < best_d2:
                best_d2, best_id = d2, sid
        if best_id is not None:
            scale = self.transform().m11() or 1.0
            if best_d2 ** 0.5 * scale <= 14:
                return best_id
        return None

    def center_on_system(self, sys: System):
        self.centerOn(self._pos[sys.id])
