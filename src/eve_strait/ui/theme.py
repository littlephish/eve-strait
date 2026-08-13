"""Spacing scale and colour tokens for everything that is not the map.

Every panel used to pick its own gutter: the character panel had none at all
so its labels sat against the dock border, the chat panel used 6, and the rest
took whatever the platform style defaulted to. Docks sitting side by side with
different gutters read as misaligned even when each one is fine on its own.

A 4px base, which is what Fluent, Material and the Qt styles all land on, so
the values agree with the native controls rather than fighting them. Named by
the job they do, not the number, so a change is one edit here.

Colour had the same problem, worse. There was no palette at all, so the map
drew its own star field at #0b0e14 inside a stock Windows light-grey shell,
and the panels around it hardcoded greys (#888) and a blue (#2a7de1) that had
clearly been picked against a dark background that was never actually there.
The tokens below are named for the role they play rather than what they look
like, so a control asks for "the surface a panel sits on" and not for a hex
value, and the whole shell can move together.
"""
from __future__ import annotations

TIGHT = 4      # inside a row of related controls
GAP = 6        # between sibling widgets in a panel
GUTTER = 8     # panel edge to content -- the one that was missing
INDENT = 14    # body of a collapsed section, to sit under its arrow

# -- surfaces, darkest first --------------------------------------------------
# Taken down from the map rather than invented: BG is the map's own background,
# so a panel reads as sitting above the star field instead of framing it.
BG = "#0b0e14"            # the map, and the window behind everything
SURFACE = "#141926"       # panels, docks, dialogs
SURFACE_ALT = "#1c2233"   # inputs, list rows, table cells
SURFACE_HI = "#273047"    # hover, alternating rows, tooltips

# -- lines --------------------------------------------------------------------
# Two, because WCAG 1.4.11 asks 3:1 of the boundary that *identifies a control*
# and nothing of a decorative divider. One token for both would either wash the
# panels out with hard rules or leave the inputs legally invisible.
BORDER = "#2b3448"        # dividers, gridlines, group outlines
BORDER_CTRL = "#596682"   # the outline that says "this is an input" - 3.05:1

# -- text ---------------------------------------------------------------------
TEXT = "#cfe3ff"          # 13.4:1 on SURFACE
TEXT_MUTED = "#93a1b5"    # 6.7:1  - captions, secondary lines
TEXT_FAINT = "#7c8a9f"    # 4.5:1  on SURFACE_ALT - placeholders, hints
TEXT_DISABLED = "#5a6577"  # deliberately below 4.5: unavailable, not unreadable

# -- meaning ------------------------------------------------------------------
# Never the only carrier of state: each of these is paired with an icon or a
# word at every call site.
ACCENT = "#4d90e8"        # interactive, and only interactive
ON_ACCENT = "#08101c"     # 5.9:1 on ACCENT
OK = "#8fd130"            # the map's own green for "you are here"
WARN = "#ffc857"
DANGER = "#ff6b6b"

# The security bands, which players read as colour before they read the number.
# Aliases rather than new values so the shell keeps one small vocabulary.
SEC_HIGH = OK
SEC_LOW = WARN
SEC_NULL = DANGER
STANDING_UP = ACCENT      # blue, as the game shows it
STANDING_DOWN = DANGER


def pad(layout, margin: int = GUTTER, spacing: int = GAP):
    """Apply the standard panel gutter and rhythm to a layout.

    Call this on a panel's root layout rather than setting margins inline, so
    a panel cannot quietly drift away from the others.
    """
    layout.setContentsMargins(margin, margin, margin, margin)
    layout.setSpacing(spacing)
    return layout


def shrinkable(combo, chars: int = 10):
    """Stop a combo box from dictating the width of its panel.

    A QComboBox sizes itself to its widest item and reports that as its
    *minimum*, so one long entry pins the whole panel wider than the dock and
    pushes everything to its right off the edge -- which is exactly what a
    "Prefer jumps - gate only when it saves several" entry did to the Find
    button. The popup still shows each item in full.
    """
    from PySide6.QtWidgets import QComboBox, QSizePolicy

    combo.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(chars)
    combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                        combo.sizePolicy().verticalPolicy())
    return combo


# -- the OS caption bar -------------------------------------------------------
# Windows draws the title bar itself, outside anything Qt can style, so a dark
# app still gets a light bar across the top of every window. DWM will recolour
# it if asked. Attribute numbers from dwmapi.h; the colour ones need Windows 11
# (build 22000+) and simply fail on anything older, which is why each call is
# allowed to fail on its own rather than gating the lot.
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36

_styled_windows: set[int] = set()


def _colorref(hexv: str) -> int:
    """Win32 wants 0x00BBGGRR, which is the reverse of what CSS gave us."""
    h = hexv.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (b << 16) | (g << 8) | r


def style_titlebar(window) -> bool:
    """Recolour one window's OS caption bar to match the chrome.

    Safe to call repeatedly and on any platform; returns whether anything was
    actually applied.
    """
    import sys

    if sys.platform != "win32" or get_chrome() == "native":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = int(window.winId())
        if not hwnd:
            return False
        dwm = ctypes.windll.dwmapi

        def attr(which: int, value: int) -> bool:
            v = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), wintypes.DWORD(which),
                ctypes.byref(v), ctypes.sizeof(v)) == 0

        # Dark mode first: on Windows 10 it is the only one that lands, and it
        # still gets the caption and the system menu out of light grey.
        ok = attr(_DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
        # Then the exact colours, so the caption matches the dock titles rather
        # than merely being dark. BG, because that is what QDockWidget::title
        # uses and the two sit directly above one another.
        ok |= attr(_DWMWA_CAPTION_COLOR, _colorref(BG))
        ok |= attr(_DWMWA_TEXT_COLOR, _colorref(TEXT))
        ok |= attr(_DWMWA_BORDER_COLOR, _colorref(BORDER))
        return ok
    except Exception:
        # An unsupported build or a locked-down dwmapi is not worth failing a
        # launch over; the window just keeps the system caption.
        return False


def _install_titlebar_hook(app):
    """Catch dialogs too, without an event filter in the hot path.

    A QApplication-wide event filter would run Python for every mouse move over
    the map, which is the one place in this app that cannot afford it.
    focusWindowChanged fires once when a window appears instead.
    """
    def on_focus(win):
        if win is None:
            return
        key = int(win.winId()) if win.winId() else 0
        if key and key not in _styled_windows:
            _styled_windows.add(key)
            style_titlebar(win)

    app.focusWindowChanged.connect(on_focus)


def _lum(hexv: str) -> float:
    h = hexv.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two hex colours."""
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _fit(hexv: str, bg: str, target: float = 4.5) -> str:
    """Walk a colour away from `bg` until it clears `target` contrast.

    Used for native chrome, where we do not know whether the platform handed
    us a light or a dark window: the same warning amber has to darken on a
    white background and lighten on a black one, and guessing wrong makes the
    text vanish rather than merely look off.
    """
    h = hexv.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    step = -6 if _lum(bg) > 0.4 else 6
    cur = hexv
    for _ in range(60):
        if contrast(cur, bg) >= target:
            return cur
        r, g, b = (min(255, max(0, v + step)) for v in (r, g, b))
        cur = f"#{r:02x}{g:02x}{b:02x}"
    return cur


def _use_native_tokens(palette):
    """Rebind the colour tokens to values that work on the platform palette.

    The panels bake these into inline stylesheets when they are constructed,
    which happens after this runs, so they pick up whichever set is live. The
    surfaces come straight from the palette; only the meaning colours need
    fitting, because a hue that reads well on the star field can disappear on
    a white window.
    """
    from PySide6.QtGui import QPalette

    r = QPalette.ColorRole
    win = palette.color(r.Window).name()
    base = palette.color(r.Base).name()
    text = palette.color(r.WindowText).name()

    g = globals()
    g["BG"] = win
    g["SURFACE"] = win
    g["SURFACE_ALT"] = base
    g["SURFACE_HI"] = palette.color(r.AlternateBase).name()
    g["BORDER"] = palette.color(r.Mid).name()
    g["BORDER_CTRL"] = palette.color(r.Dark).name()
    g["TEXT"] = text
    # Muted and faint are the platform's text colour walked toward the
    # background, so they stay secondary without dropping below 4.5:1.
    g["TEXT_MUTED"] = _fit(_blend(text, win, 0.35), win, 4.5)
    g["TEXT_FAINT"] = _fit(_blend(text, base, 0.45), base, 4.5)
    g["TEXT_DISABLED"] = palette.color(QPalette.ColorGroup.Disabled,
                                       r.WindowText).name()
    g["ACCENT"] = _fit(palette.color(r.Highlight).name(), win, 4.5)
    g["ON_ACCENT"] = palette.color(r.HighlightedText).name()
    for name, seed in (("OK", "#2e7d32"), ("WARN", "#b26a00"),
                       ("DANGER", "#c62828")):
        g[name] = _fit(seed, win, 4.5)
    g["SEC_HIGH"], g["SEC_LOW"], g["SEC_NULL"] = g["OK"], g["WARN"], g["DANGER"]
    g["STANDING_UP"], g["STANDING_DOWN"] = g["ACCENT"], g["DANGER"]


def _blend(a: str, b: str, t: float) -> str:
    """`t` of the way from a to b."""
    pa = [int(a.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    pb = [int(b.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


# Captured the first time apply_theme runs, before anything is overridden, so
# "native" restores what the platform actually gave us rather than an
# approximation of it.
_NATIVE: dict = {}
_DARK_TOKENS = {k: v for k, v in globals().items()
                if k.isupper() and isinstance(v, str) and v.startswith("#")}
_DARK_TOKENS.update(SEC_HIGH=OK, SEC_LOW=WARN, SEC_NULL=DANGER,
                    STANDING_UP=ACCENT, STANDING_DOWN=DANGER)


def apply_theme(app, mode: str | None = None):
    """Install the chrome. `mode` is "dark" (default) or "native".

    Native hands the shell back to the platform style and palette. It exists
    because a hardcoded dark theme overrides whatever the user has set at the
    OS level -- high contrast, forced colours, an increased-contrast palette --
    and for someone who depends on those, ignoring them is worse than being
    ugly. The map keeps its own star field either way; it is a chart, not
    chrome.
    """
    from PySide6.QtGui import QPalette

    if not _NATIVE:
        _NATIVE["palette"] = QPalette(app.palette())
        _NATIVE["sheet"] = app.styleSheet()
        _NATIVE["style"] = _style_key(app)

    if mode is None:
        mode = get_chrome()

    if mode == "native":
        app.setStyle(_NATIVE["style"])
        app.setPalette(_NATIVE["palette"])
        app.setStyleSheet(_NATIVE["sheet"])
        _use_native_tokens(app.palette())
        return app

    globals().update(_DARK_TOKENS)
    _apply_dark(app)
    _install_titlebar_hook(app)
    return app


def _style_key(app) -> str:
    """The QStyleFactory key for the style currently installed."""
    name = app.style().metaObject().className()      # e.g. QWindowsVistaStyle
    return name.removeprefix("Q").removesuffix("Style") or "Fusion"


def get_chrome() -> str:
    """"dark" (default) or "native"."""
    try:
        from .. import config
        return config.get_settings().get("chrome", "dark")
    except Exception:
        return "dark"


def set_chrome(mode: str) -> None:
    from .. import config
    s = config.get_settings()
    s["chrome"] = mode
    config.save_settings(s)


def _apply_dark(app):
    """Dress the whole application, not just the widgets we remembered to style.

    Fusion rather than the native Windows style: the native style draws its own
    themed backgrounds and ignores the palette for a lot of controls, so a dark
    palette on it gives you dark panels with light-grey combo boxes and white
    scrollbars. Fusion honours the palette everywhere, including the disabled
    colour group, which is what makes a greyed-out control still look greyed
    out rather than merely dim.

    So the palette does the work and the stylesheet stays small - it only
    covers what a palette cannot say: dock title bars, focus rings, the section
    headers, and the one primary button.
    """
    from PySide6.QtGui import QColor, QPalette

    app.setStyle("Fusion")

    p = QPalette()
    g = QPalette.ColorGroup
    r = QPalette.ColorRole

    p.setColor(r.Window, QColor(SURFACE))
    p.setColor(r.WindowText, QColor(TEXT))
    p.setColor(r.Base, QColor(SURFACE_ALT))
    p.setColor(r.AlternateBase, QColor(SURFACE_HI))
    p.setColor(r.Text, QColor(TEXT))
    p.setColor(r.PlaceholderText, QColor(TEXT_FAINT))
    p.setColor(r.Button, QColor(SURFACE_ALT))
    p.setColor(r.ButtonText, QColor(TEXT))
    p.setColor(r.ToolTipBase, QColor(SURFACE_HI))
    p.setColor(r.ToolTipText, QColor(TEXT))
    p.setColor(r.Highlight, QColor(ACCENT))
    p.setColor(r.HighlightedText, QColor(ON_ACCENT))
    p.setColor(r.Link, QColor(ACCENT))
    p.setColor(r.LinkVisited, QColor(ACCENT))
    p.setColor(r.BrightText, QColor(DANGER))
    # Fusion draws its bevels and frames from these three.
    p.setColor(r.Light, QColor(SURFACE_HI))
    p.setColor(r.Mid, QColor(BORDER))
    p.setColor(r.Dark, QColor(BG))
    p.setColor(r.Shadow, QColor(BG))

    # Disabled has to be set explicitly or Fusion derives a washed-out grey
    # from the *active* colours and unavailable controls stop reading as
    # unavailable.
    for role in (r.WindowText, r.Text, r.ButtonText, r.HighlightedText):
        p.setColor(g.Disabled, role, QColor(TEXT_DISABLED))
    p.setColor(g.Disabled, r.Base, QColor(SURFACE))
    p.setColor(g.Disabled, r.Button, QColor(SURFACE))
    p.setColor(g.Disabled, r.Highlight, QColor(BORDER))

    app.setPalette(p)
    app.setStyleSheet(_QSS)
    return app


# Only the things the palette has no role for. Anything expressible as a
# palette colour belongs above, so it applies to widgets nobody thought to
# name here.
_QSS = f"""
QMainWindow, QDialog {{ background: {SURFACE}; }}

/* The dock title is a label, not a control: quiet, and clearly not clickable
   in the way the buttons below it are. */
QDockWidget {{
    titlebar-close-icon: none;
    font-weight: 600;
    color: {TEXT_MUTED};
}}
QDockWidget::title {{
    background: {BG};
    padding: 6px {GUTTER}px;
    border-bottom: 1px solid {BORDER};
}}

/* Inputs carry a real outline: their fill is only a shade off the panel, so
   without it there is nothing saying where you may type. */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER_CTRL};
    border-radius: 3px;
    padding: 3px 6px;
    selection-background-color: {ACCENT};
    selection-color: {ON_ACCENT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {ACCENT};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    background: {SURFACE};
    border-color: {BORDER};
}}
QComboBox QAbstractItemView {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER_CTRL};
    selection-background-color: {ACCENT};
    selection-color: {ON_ACCENT};
}}

QPushButton {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER_CTRL};
    border-radius: 3px;
    padding: 5px 10px;
}}
QPushButton:hover {{ background: {SURFACE_HI}; }}
QPushButton:pressed {{ background: {BG}; }}
QPushButton:disabled {{ background: {SURFACE}; border-color: {BORDER}; }}
/* One accent button per screen, so the accent keeps meaning "start here".
   Set with setObjectName("primary"). */
QPushButton#primary {{
    background: {ACCENT};
    color: {ON_ACCENT};
    border: 1px solid {ACCENT};
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: #5b9bee; border-color: #5b9bee; }}
QPushButton#primary:pressed {{ background: #3f7fd0; border-color: #3f7fd0; }}
/* A flat button is a link in disguise; give it link colour, not button chrome. */
QPushButton:flat {{
    background: transparent;
    border: none;
    color: {ACCENT};
    text-align: left;
}}
QPushButton:flat:hover {{ text-decoration: underline; }}

QListWidget, QTreeWidget, QTableWidget, QTableView {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    alternate-background-color: {SURFACE_HI};
}}
QListWidget::item, QTableWidget::item {{ padding: 2px 4px; }}
QListWidget::item:selected, QTableWidget::item:selected {{
    background: {ACCENT};
    color: {ON_ACCENT};
}}
QHeaderView::section {{
    background: {SURFACE};
    color: {TEXT_MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    border-right: 1px solid {BORDER};
    padding: 4px 6px;
    font-weight: 600;
}}

/* Section headers read as headings; the arrow already says "expandable". */
QToolButton {{
    background: transparent;
    border: none;
    padding: 3px 2px;
    color: {TEXT};
    font-weight: 600;
}}
QToolButton:hover {{ color: {ACCENT}; }}
QToolButton:disabled {{ color: {TEXT_DISABLED}; }}

QCheckBox, QRadioButton {{ spacing: {TIGHT + 2}px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {BORDER_CTRL};
    border-radius: 3px;
    background: {SURFACE_ALT};
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    border-color: {BORDER};
    background: {SURFACE};
}}

QProgressBar {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 3px;
    text-align: center;
    color: {TEXT_MUTED};
    height: 16px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}

QMenuBar, QMenu {{ background: {SURFACE}; }}
QMenuBar::item:selected, QMenu::item:selected {{
    background: {ACCENT}; color: {ON_ACCENT};
}}
QMenu {{ border: 1px solid {BORDER_CTRL}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 0; }}

QStatusBar {{ background: {BG}; color: {TEXT_MUTED}; }}
QStatusBar::item {{ border: none; }}

QToolTip {{
    background: {SURFACE_HI};
    color: {TEXT};
    border: 1px solid {BORDER_CTRL};
    padding: 4px 6px;
}}

QScrollBar:vertical, QScrollBar:horizontal {{
    background: transparent; border: none;
}}
QScrollBar:vertical {{ width: 11px; }}
QScrollBar:horizontal {{ height: 11px; }}
QScrollBar::handle {{ background: {BORDER_CTRL}; border-radius: 5px; }}
QScrollBar::handle:hover {{ background: {TEXT_FAINT}; }}
QScrollBar::handle:vertical {{ min-height: 24px; }}
QScrollBar::handle:horizontal {{ min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSplitter::handle {{ background: {BORDER}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; }}
QTabBar::tab {{
    background: {SURFACE};
    color: {TEXT_MUTED};
    border: 1px solid {BORDER};
    padding: 5px 12px;
}}
QTabBar::tab:selected {{
    background: {SURFACE_ALT};
    color: {TEXT};
    border-bottom: 2px solid {ACCENT};
}}
"""


def compressible(widget, floor: int = 60):
    """Let a control shrink below the width of its own text.

    QCheckBox and QLabel report their full text width as their *minimum*, so
    one long label pins the whole panel wider than the dock and pushes
    everything to its right off the edge. Setting an explicit minimum width
    overrides that hint, so the layout may compress the control and the panel
    can follow the dock. Keep the label short as well -- this stops a long one
    breaking the layout, it does not make it readable.
    """
    widget.setMinimumWidth(floor)
    return widget
