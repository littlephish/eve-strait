"""2D pan/zoom map of New Eden with region labels, range and route overlays."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QTransform
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from ..data.universe import System, Universe

_IGNORE_XF = QGraphicsEllipseItem.GraphicsItemFlag.ItemIgnoresTransformations


def _anchor_px(item, scene_pt: QPointF, dx: float, dy: float):
    """Anchor an ItemIgnoresTransformations item at a scene point, offset by
    (dx, dy) *screen pixels*.

    The position is in scene units (light years), so the pixel offset must be
    applied via the item's own transform -- putting it in setPos() would scale
    the gap with the zoom level and fling the label away from its system.
    """
    item.setFlag(_IGNORE_XF, True)
    item.setPos(scene_pt)
    item.setTransform(QTransform.fromTranslate(dx, dy))


# EVE's security-status colour scale: one colour per 0.1 step, running
# blue (1.0) -> cyan -> green -> yellow -> orange -> red (0.0), with deep red
# for negative (null) security. Keyed by security * 10, rounded like the game.
_SEC_COLORS = {
    10: "#2C74E8",   # 1.0  deep blue
    9: "#3D9BE9",    # 0.9  blue
    8: "#4FC3F7",    # 0.8  light blue
    7: "#2BC4A5",    # 0.7  teal
    6: "#3FBF3F",    # 0.6  green
    5: "#8FD130",    # 0.5  lime
    4: "#E8E216",    # 0.4  yellow
    3: "#F0A020",    # 0.3  amber
    2: "#F06A10",    # 0.2  orange
    1: "#E8471C",    # 0.1  red-orange
    0: "#D42B2B",    # 0.0  red
}
_NULL_COLOR = QColor("#8E1F1F")      # below 0.0


def _sec_color(sec: float) -> QColor:
    """Colour for a security status, matching the in-game map scale."""
    if sec <= 0.0:
        return _NULL_COLOR
    step = max(0, min(10, int(round(sec * 10))))
    return QColor(_SEC_COLORS[step])


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
        # Items with ItemIgnoresTransformations confuse Qt's dirty-region
        # tracking and leave paint trails; repaint the whole viewport instead.
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.FullViewportUpdate)

        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        self._pos: dict[int, QPointF] = {}
        self._overlay: list = []
        self._labels: list = []
        self._panning = False
        self._pan_start = QPointF()
        self._moved = False
        self._hover_id = None
        self._sov_lookup = None

        # The View menu flips these and every redraw re-applies them, so a
        # background intel refresh can't bring a layer the user switched off
        # back to life. Kill rings are off by default: nearly 3000 systems
        # see a kill in any given hour, so leaving them on buries the map.
        self._overlay_on = {"gates": True, "bridges": True, "regions": True,
                            "kills": False, "avoid": True, "location": True,
                            "notes": True, "heat": True, "sov": False}
        self._sov_items: list = []
        self._dots: dict[int, QGraphicsEllipseItem] = {}
        self._sec_brushes: dict[int, QBrush] = {}
        self._heat_brushes: dict[int, QBrush] = {}
        self._heat_label = ""
        self._heat_max = 0.0

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
    def _build_gate_links(self):
        """Draw stargate connections, coloured by the security of the link.

        ~13k segments would be far too many individual items to pan smoothly,
        so they are batched into one painter path per security class.
        """
        from PySide6.QtGui import QPainterPath
        from PySide6.QtWidgets import QGraphicsPathItem

        systems = self.universe.systems
        # One path per security step (-1 == null) so links carry the same
        # colour scale as the systems they connect.
        paths: dict[int, QPainterPath] = {}
        seen: set[tuple[int, int]] = set()
        for a_id, neighbours in self.universe.gates.items():
            a = systems.get(a_id)
            if a is None:
                continue
            for b_id in neighbours:
                if a_id == b_id:
                    continue
                pair = (a_id, b_id) if a_id < b_id else (b_id, a_id)
                if pair in seen:
                    continue
                seen.add(pair)
                b = systems.get(b_id)
                if b is None:
                    continue
                # Classify by the *lower* security of the two ends: that's the
                # risk of taking the gate.
                sec = min(a.security, b.security)
                key = -1 if sec <= 0.0 else max(0, min(10, int(round(sec * 10))))
                path = paths.get(key)
                if path is None:
                    path = paths[key] = QPainterPath()
                path.moveTo(a.x, -a.z)
                path.lineTo(b.x, -b.z)

        self._gate_items = []
        for key, path in sorted(paths.items()):
            if path.isEmpty():
                continue
            colour = QColor(_NULL_COLOR if key < 0 else _SEC_COLORS[key])
            colour.setAlpha(120)          # dimmed so system dots stay dominant
            item = QGraphicsPathItem(path)
            pen = QPen(colour, 0.9)
            pen.setCosmetic(True)          # constant width regardless of zoom
            item.setPen(pen)
            item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            item.setZValue(-2)             # under region labels and dots
            self.scene_obj.addItem(item)
            self._gate_items.append(item)

    def set_sov_lookup(self, fn):
        """Install a callable(system_id) -> owner name, shown on hover."""
        self._sov_lookup = fn
        self._hover_id = None      # force the next hover to re-render

    def set_kill_activity(self, kills: dict):
        """Ring systems with recent player kills, sized by how many.

        Drawn as one batched path per severity band so 2900 systems of data
        cost a handful of scene items rather than thousands.
        """
        from PySide6.QtGui import QPainterPath
        from PySide6.QtWidgets import QGraphicsPathItem

        for item in getattr(self, "_kill_items", ()):
            self.scene_obj.removeItem(item)
        self._kill_items = []
        if not kills:
            return

        # (min ship kills, radius, colour)
        bands = ((20, 7.0, QColor(255, 60, 60, 210)),
                 (5, 5.5, QColor(255, 140, 40, 190)),
                 (1, 4.0, QColor(255, 210, 60, 160)))
        paths = {i: QPainterPath() for i in range(len(bands))}
        for sid, counts in kills.items():
            ship = counts.get("ship", 0)
            if ship < 1:
                continue
            p = self._pos.get(sid)
            if p is None:
                continue
            for i, (threshold, radius, _) in enumerate(bands):
                if ship >= threshold:
                    paths[i].addEllipse(p.x() - radius, p.y() - radius,
                                        2 * radius, 2 * radius)
                    break
        for i, (_, _, colour) in enumerate(bands):
            if paths[i].isEmpty():
                continue
            item = QGraphicsPathItem(paths[i])
            pen = QPen(colour, 1.4)
            pen.setCosmetic(True)
            item.setPen(pen)
            item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            item.setZValue(2.5)
            self.scene_obj.addItem(item)
            self._kill_items.append(item)
        self._apply_visibility("kills")

    def set_kill_lookup(self, fn):
        """callable(system_id) -> dict of kill counts, shown on hover."""
        self._kill_lookup = fn
        self._hover_id = None

    def set_current_location(self, system_id):
        """Mark where the active character is with a cyan 'you are here' ring."""
        for item in getattr(self, "_here_items", ()):
            self.scene_obj.removeItem(item)
        self._here_items = []
        p = self._pos.get(system_id)
        if p is None:
            return
        for radius, width in ((9.0, 2.0), (13.0, 1.0)):
            ring = QGraphicsEllipseItem(-radius, -radius, 2 * radius, 2 * radius)
            ring.setPos(p)
            ring.setPen(QPen(QColor("#00e5ff"), width))
            ring.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            ring.setFlag(_IGNORE_XF, True)
            ring.setZValue(6)
            self.scene_obj.addItem(ring)
            self._here_items.append(ring)
        self._apply_visibility("location")

    # Influence radius in light years. Deliberately far larger than the 0.4 ly
    # median spacing between sovereign systems: influence maps are read as
    # *territory*, so a holding should reach outward until something stops it
    # rather than hugging its own constellations. What stops it is empire
    # space, which is punched back out afterwards, and the edge of the map.
    SOV_RADIUS = 4.5
    # Resolution of the baked territory layer. 26 px/ly stays crisp well past
    # the zoom levels territory is actually looked at; the cap bounds memory
    # on the ~90 x 100 ly sovereign area to a few tens of MB.
    SOV_PIXELS_PER_LY = 26.0
    SOV_MAX_PIXELS = 2800
    SOV_ALPHA = 0.42          # washed enough that the star field reads through
    SOV_EMPIRE_RADIUS = 4.2   # how far empire space pushes sovereignty back
    SOV_BORDER_LY = 0.16      # dilation that forms the outer rim

    def set_sov_territory(self, groups, radius: float | None = None):
        """Fill each owner's space as one continuous blob.

        ``groups`` is [(QColor, [system_id, ...]), ...], one entry per owner.

        Each system contributes a disc and the discs are merged with
        QPainterPath.simplified(), which resolves the overlaps into a single
        outline. Drawing the discs individually would be far more subpaths to
        rasterise every frame, and every internal arc would show through as a
        seam once the shape is outlined.

        A convex hull would be wrong here, not just slower: holdings are
        concave and interleaved, so a hull would swallow a neighbour's space.
        """
        baked = self.bake_sov_image(
            [(c, [self._pos[s] for s in ids if s in self._pos])
             for c, ids in groups],
            self.SOV_RADIUS if radius is None else radius)
        self.set_sov_image(baked)

    @classmethod
    def bake_sov_image(cls, groups, radius: float, empire=()):
        """Merge and rasterise the territory. Safe to call off the UI thread.

        ``groups`` is [(QColor, [QPointF, ...], [(QPointF, QPointF), ...]), ...]
        -- one entry per owner, in draw order: its systems, and the gate links
        between systems it owns. Each owner gets its own colour, so later
        entries overwrite earlier ones where territory is contested.

        ``empire`` is the high and low-sec systems. Sovereignty is drawn with a
        wide reach and then cleared back around them, which is what gives the
        border along empire space and lets holdings run out to the edge of the
        map everywhere else. Drawing the reach small enough to avoid empire in
        the first place would leave the holdings hugging their own
        constellations with black between them. Returns (QImage, top_left QPointF, px_per_ly) or None.

        Each system contributes a disc and each internal gate link a thick
        capsule, so the shape follows the owner's actual topology and reads as
        one connected territory rather than beads on a string.

        Three things this deliberately does NOT do, each learned the hard way:

        * No stroking of the shapes. The discs overlap, so a pen traces every
          internal arc and draws a lattice of circles instead of a border. The
          border is a dilation: the same geometry drawn larger underneath.
        * No QPainterPath.simplified(). It does not reduce the discs to one
          outline, it splits them at every intersection, and building the
          border from a stroker union on top of that took 45 seconds.
        * No single giant path per owner. Rasterising 500 overlapping subpaths
          under the winding rule is far slower than 500 independent opaque
          fills that simply overwrite each other.

        Colours are composited one at a time through an opaque mask. Painting
        translucent shapes straight onto the canvas makes every overlap darker,
        so two neighbours sharing a colour show a seam and connected space
        stops looking connected.

        QImage rather than QPixmap: QPixmap may only be touched on the GUI
        thread, and this takes long enough to need a worker.
        """
        from PySide6.QtGui import QImage

        clean = [(QColor(c), pts, links) for c, pts, links in groups if pts]
        if not clean:
            return None

        wide_r = radius + cls.SOV_BORDER_LY
        xs = [p.x() for _, pts, _ in clean for p in pts]
        ys = [p.y() for _, pts, _ in clean for p in pts]
        bounds = QRectF(min(xs) - wide_r, min(ys) - wide_r,
                        max(xs) - min(xs) + 2 * wide_r,
                        max(ys) - min(ys) + 2 * wide_r)

        px_per_ly = min(cls.SOV_MAX_PIXELS / max(bounds.width(), bounds.height()),
                        cls.SOV_PIXELS_PER_LY)
        size = (max(1, int(bounds.width() * px_per_ly)),
                max(1, int(bounds.height() * px_per_ly)))

        image = QImage(*size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        mask = QImage(*size, QImage.Format.Format_ARGB32_Premultiplied)
        mask.fill(Qt.GlobalColor.transparent)

        def blob(mp, pts, links, r, colour):
            pen = QPen(colour, 2 * r, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            mp.setBrush(QBrush(colour))
            mp.setPen(pen)
            for a, b in links:              # corridors along gate links
                mp.drawLine(a, b)
            mp.setPen(Qt.PenStyle.NoPen)
            for p in pts:
                mp.drawEllipse(p, r, r)

        # Everything goes into one opaque mask, then the mask is composited
        # once. Painting translucent shapes straight onto the canvas makes
        # every overlap darker, so an alliance's own discs and corridors show
        # seams where they meet and its space stops looking connected. Opaque,
        # they simply coincide. Between alliances the later colour overwrites,
        # which gives a clean border rather than a muddy blend.
        mp = QPainter(mask)
        mp.setRenderHint(QPainter.RenderHint.Antialiasing)
        mp.scale(px_per_ly, px_per_ly)
        mp.translate(-bounds.left(), -bounds.top())
        for colour, pts, links in clean:
            blob(mp, pts, links, wide_r, colour.lighter(150))   # border under
        for colour, pts, links in clean:
            blob(mp, pts, links, radius, colour)                # interior over
        # Punch empire space back out. CompositionMode_Clear rather than
        # painting the background colour: the mask is composited translucently,
        # so anything painted here would tint the map instead of revealing it.
        if empire:
            mp.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            mp.setPen(Qt.PenStyle.NoPen)
            mp.setBrush(QBrush(Qt.GlobalColor.black))
            r = cls.SOV_EMPIRE_RADIUS
            for p in empire:
                mp.drawEllipse(p, r, r)
        mp.end()

        painter = QPainter(image)
        painter.setOpacity(cls.SOV_ALPHA)
        painter.drawImage(0, 0, mask)
        painter.end()
        return image, bounds.topLeft(), px_per_ly

    def set_sov_labels(self, labels):
        """Name each territory. ``labels`` is [(text, QPointF, QColor), ...].

        Added to the same item list as the territory image so the View menu
        toggle covers both, and drawn ignoring the view transform so the names
        stay legible at any zoom, like the region labels.
        """
        for text, point, colour, weight in labels:
            item = QGraphicsSimpleTextItem(text)
            item.setBrush(QBrush(QColor(colour).lighter(160)))
            font = QFont()
            # Size by holding, so the map reads at a glance: the bloc that owns
            # a third of null-sec should not be labelled like a five-system
            # renter.
            font.setPointSize(max(7, min(22, int(7 + 15 * weight ** 0.5))))
            font.setBold(True)
            item.setFont(font)
            item.setFlag(_IGNORE_XF, True)
            item.setZValue(-0.5)      # over the fill, under the system dots
            # Centre the name on the territory rather than hanging it off the
            # corner: these mark an area, not a point.
            rect = item.boundingRect()
            _anchor_px(item, point, -rect.width() / 2, -rect.height() / 2)
            self.scene_obj.addItem(item)
            self._sov_items.append(item)
        self._apply_visibility("sov")

    def set_sov_image(self, baked):
        """Install a baked territory image. UI thread only."""
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import QGraphicsPixmapItem

        for item in self._sov_items:
            self.scene_obj.removeItem(item)
        self._sov_items = []
        if baked is None:
            return
        image, top_left, px_per_ly = baked
        item = QGraphicsPixmapItem(QPixmap.fromImage(image))
        item.setPos(top_left)
        item.setScale(1.0 / px_per_ly)
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        item.setZValue(-3)                 # under the gate mesh and everything
        self.scene_obj.addItem(item)
        self._sov_items.append(item)
        self._apply_visibility("sov")

    def set_noted(self, system_ids):
        """Mark systems you have written a note about with a small tag.

        Drawn above the dot so it survives whatever the heat layer is doing to
        the dot colour underneath.
        """
        for item in getattr(self, "_note_items", ()):
            self.scene_obj.removeItem(item)
        self._note_items = []
        for sid in system_ids:
            p = self._pos.get(sid)
            if p is None:
                continue
            tag = QGraphicsEllipseItem(-2.6, -2.6, 5.2, 5.2)
            tag.setPos(p.x() + 6.5, p.y() - 6.5)
            tag.setBrush(QBrush(QColor("#ffd479")))
            tag.setPen(QPen(QColor("#3a2c08"), 1.2))
            tag.setFlag(_IGNORE_XF, True)
            tag.setZValue(5.5)
            self.scene_obj.addItem(tag)
            self._note_items.append(tag)
        self._apply_visibility("notes")

    def set_note_lookup(self, fn):
        """callable(system_id) -> note text, shown on hover."""
        self._note_lookup = fn
        self._hover_id = None

    def set_avoided(self, system_ids):
        """Mark avoided systems with a red X so they're obvious on the map."""
        for item in getattr(self, "_avoid_items", ()):
            self.scene_obj.removeItem(item)
        self._avoid_items = []
        for sid in system_ids:
            p = self._pos.get(sid)
            if p is None:
                continue
            for dx, dy in ((-5, -5, ), (-5, 5)):
                line = QGraphicsLineItem(dx, dy, -dx, -dy)
                line.setPos(p)
                pen = QPen(QColor("#ff4d4d"), 2.0)
                line.setPen(pen)
                line.setFlag(_IGNORE_XF, True)
                line.setZValue(6)
                self.scene_obj.addItem(line)
                self._avoid_items.append(line)
        self._apply_visibility("avoid")

    def refresh_bridges(self):
        """(Re)draw Ansiblex jump-gate links in purple, over the gate mesh."""
        from PySide6.QtGui import QPainterPath
        from PySide6.QtWidgets import QGraphicsPathItem

        for item in getattr(self, "_bridge_items", ()):
            self.scene_obj.removeItem(item)
        self._bridge_items = []

        systems = self.universe.systems
        path = QPainterPath()
        seen: set[tuple[int, int]] = set()
        for a_id, targets in self.universe.bridges.items():
            a = systems.get(a_id)
            if a is None:
                continue
            for b_id in targets:
                pair = (a_id, b_id) if a_id < b_id else (b_id, a_id)
                if pair in seen:
                    continue
                seen.add(pair)
                b = systems.get(b_id)
                if b is None:
                    continue
                path.moveTo(a.x, -a.z)
                path.lineTo(b.x, -b.z)
        if path.isEmpty():
            return
        item = QGraphicsPathItem(path)
        pen = QPen(QColor(178, 102, 255, 200), 1.5)   # Ansiblex purple
        pen.setCosmetic(True)
        item.setPen(pen)
        item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        item.setZValue(-1.5)
        self.scene_obj.addItem(item)
        self._bridge_items.append(item)
        self._apply_visibility("bridges")

    def set_gate_links_visible(self, visible: bool):
        self.set_overlay_visible("gates", visible)

    # -- heat map ----------------------------------------------------------
    # Ramp for the value scale, coldest first.
    _HEAT_RAMP = ("#2b4a8f", "#1f7a8c", "#3fa34d", "#c9b62a",
                  "#e07a1f", "#d63b2a")

    # Systems with no reading, so the ones that do have data stand out.
    _HEAT_COLD = QColor(52, 58, 74)

    def set_heat(self, values: dict | None, label: str = ""):
        """Shade the system dots by one metric.

        Deliberately recolours the existing dots rather than adding a halo
        layer. 5000 extra filled ellipses cost about 285 ms of rasterising
        per frame, and this view repaints in full on every mouse move, so a
        separate layer made the map feel frozen. Recolouring is free at paint
        time: the dots are drawn either way.

        Scaled logarithmically, because kills and gate traffic are so skewed
        that a linear ramp paints Jita red and leaves everything else in an
        identical blue. Pass None or an empty dict to clear the layer.
        """
        import math

        self._heat_brushes = {}
        self._heat_label = label
        self._heat_max = 0.0

        clean = {sid: float(v) for sid, v in (values or {}).items()
                 if v and float(v) > 0 and sid in self._dots}
        if clean:
            top = max(clean.values())
            self._heat_max = top
            span = math.log10(1.0 + top) or 1.0
            n = len(self._HEAT_RAMP)
            # One brush per band, shared by every system in it.
            band_brushes = [QBrush(QColor(c)) for c in self._HEAT_RAMP]
            for sid, v in clean.items():
                band = min(n - 1, int(math.log10(1.0 + v) / span * n))
                self._heat_brushes[sid] = band_brushes[band]
        self._apply_heat()

    def _apply_heat(self):
        """Repaint the dots as either the heat ramp or security colours."""
        on = bool(self._heat_brushes) and self._overlay_on.get("heat", True)
        cold = QBrush(self._HEAT_COLD)
        for sid, dot in self._dots.items():
            if on:
                dot.setBrush(self._heat_brushes.get(sid, cold))
            else:
                dot.setBrush(self._sec_brushes[sid])
        self.viewport().update()

    def set_overlay_visible(self, name: str, visible: bool):
        """Show or hide one map layer by name."""
        self._overlay_on[name] = bool(visible)
        if name == "heat":
            self._apply_heat()
            return
        for item in self._items_for(name):
            item.setVisible(bool(visible))

    def overlay_visible(self, name: str) -> bool:
        return self._overlay_on.get(name, True)

    def _items_for(self, name: str):
        return {
            "gates": getattr(self, "_gate_items", ()),
            "bridges": getattr(self, "_bridge_items", ()),
            "regions": getattr(self, "_region_items", ()),
            "kills": getattr(self, "_kill_items", ()),
            "avoid": getattr(self, "_avoid_items", ()),
            "location": getattr(self, "_here_items", ()),
            "notes": getattr(self, "_note_items", ()),
            "sov": self._sov_items,
        }.get(name, ())

    def _apply_visibility(self, name: str):
        """Re-assert a layer's state after it has been redrawn."""
        if self._overlay_on.get(name, True):
            return
        for item in self._items_for(name):
            item.setVisible(False)

    def _build(self):
        self._build_gate_links()

        # Region labels (behind everything).
        self._region_items = []
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
            self._region_items.append(t)

        for s in self.universe.systems.values():
            x, y = s.x, -s.z  # north up
            self._pos[s.id] = QPointF(x, y)
            dot = QGraphicsEllipseItem(-2.0, -2.0, 4.0, 4.0)
            dot.setPos(x, y)
            brush = QBrush(_sec_color(s.security))
            dot.setBrush(brush)
            dot.setPen(QPen(Qt.PenStyle.NoPen))
            dot.setFlag(_IGNORE_XF, True)
            dot.setToolTip(f"{s.name}  ({s.security:.1f})")
            dot.setZValue(1)
            self.scene_obj.addItem(dot)
            self._dots[s.id] = dot
            self._sec_brushes[s.id] = brush
        rect = self.scene_obj.itemsBoundingRect()
        self.scene_obj.setSceneRect(rect.adjusted(-20, -20, 20, 20))
        self.resetTransform()
        self.scale(6.0, 6.0)
        # Open centred on Jita (trade hub) rather than the geometric middle.
        jita = self.universe.by_name("Jita")
        self.centerOn(self._pos[jita.id] if jita else rect.center())

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
            _anchor_px(t, p, radius + 4, -6)
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
        label = self.universe.systems[sid].name
        if self._sov_lookup is not None:
            owner = self._sov_lookup(sid)
            if owner:
                label = f"{label}  -  {owner}"
        lookup = getattr(self, "_kill_lookup", None)
        if lookup is not None:
            k = lookup(sid) or {}
            if k.get("ship") or k.get("pod"):
                label = (f"{label}  |  {k.get('ship', 0)} kills, "
                         f"{k.get('pod', 0)} pods (1h)")
        notes = getattr(self, "_note_lookup", None)
        if notes is not None:
            note = (notes(sid) or "").strip()
            if note:
                # Your own note outranks everything else on the line.
                one_line = " ".join(note.split())
                if len(one_line) > 70:
                    one_line = one_line[:67] + "..."
                label = f"{label}\n✎ {one_line}"
        self._hover_text.setText(label)
        _anchor_px(self._hover_text, p, 11, 4)
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

    # -- scale bar ----------------------------------------------------------
    @staticmethod
    def _nice_ly(value: float) -> float:
        """Round to a friendly 1/2/5 x 10^n figure for the scale bar."""
        import math
        if value <= 0:
            return 1.0
        exp = math.floor(math.log10(value))
        base = 10.0 ** exp
        for mult in (1, 2, 5):
            if value <= mult * base:
                return mult * base
        return 10 * base

    def drawForeground(self, painter, rect):
        """Light-year scale bar, pinned to the bottom-left of the viewport."""
        super().drawForeground(painter, rect)
        px_per_ly = self.transform().m11()
        if px_per_ly <= 0:
            return
        ly = self._nice_ly(110.0 / px_per_ly)     # aim for ~110 px
        width = ly * px_per_ly

        painter.save()
        painter.resetTransform()                  # draw in viewport pixels
        h = self.viewport().height()
        x0, y0 = 14.0, h - 20.0

        pen = QPen(QColor("#cfe3ff"), 1.6)
        painter.setPen(pen)
        painter.drawLine(QPointF(x0, y0), QPointF(x0 + width, y0))
        for x in (x0, x0 + width):                # end ticks
            painter.drawLine(QPointF(x, y0 - 4), QPointF(x, y0 + 4))

        f = QFont()
        f.setPointSize(9)
        f.setBold(True)
        painter.setFont(f)
        label = f"{ly:g} ly"
        painter.drawText(QPointF(x0 + width + 8, y0 + 4), label)

        # The dots carry the heat ramp while a heat layer is on, so showing
        # the security key at the same time would be a lie.
        if self._heat_brushes and self._overlay_on.get("heat", True):
            self._draw_heat_legend(painter, x0, y0 - 26)
        else:
            self._draw_sec_legend(painter, x0, y0 - 26)
        painter.restore()

    def _draw_heat_legend(self, painter, x0: float, y_bottom: float):
        """Key for the active heat layer: the ramp, its name and its peak."""
        sw, h = 15.0, 8.0
        f = QFont()
        f.setPointSize(8)
        painter.setFont(f)

        x = x0
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        for colour in self._HEAT_RAMP:
            painter.fillRect(QRectF(x, y_bottom - h, sw, h), QColor(colour))
            x += sw

        painter.setPen(QPen(QColor("#cfe3ff")))
        top = self._heat_max
        peak = f"{top:,.1f}".rstrip("0").rstrip(".") if top < 10 else f"{top:,.0f}"
        painter.drawText(QPointF(x + 6, y_bottom - 1), f"max {peak}")
        painter.drawText(QPointF(x0, y_bottom - h - 3), self._heat_label or "heat")

    def _draw_sec_legend(self, painter, x0: float, y_bottom: float):
        """Security colour key: one swatch per 0.1 step, plus null."""
        sw, h = 13.0, 8.0
        f = QFont()
        f.setPointSize(8)
        painter.setFont(f)
        painter.setPen(QPen(Qt.PenStyle.NoPen))

        x = x0
        for step in range(10, -1, -1):          # 1.0 -> 0.0
            painter.fillRect(QRectF(x, y_bottom - h, sw, h), QColor(_SEC_COLORS[step]))
            x += sw
        x += 4
        painter.fillRect(QRectF(x, y_bottom - h, sw, h), _NULL_COLOR)

        painter.setPen(QPen(QColor("#cfe3ff")))
        painter.drawText(QPointF(x0, y_bottom - h - 3), "1.0")
        painter.drawText(QPointF(x0 + 10 * sw - 8, y_bottom - h - 3), "0.0")
        painter.drawText(QPointF(x + 1, y_bottom - h - 3), "null")

    def center_on_system(self, sys: System):
        self.centerOn(self._pos[sys.id])
