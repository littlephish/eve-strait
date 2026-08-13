"""Spacing scale for the panels.

Every panel used to pick its own gutter: the character panel had none at all
so its labels sat against the dock border, the chat panel used 6, and the rest
took whatever the platform style defaulted to. Docks sitting side by side with
different gutters read as misaligned even when each one is fine on its own.

A 4px base, which is what Fluent, Material and the Qt styles all land on, so
the values agree with the native controls rather than fighting them. Named by
the job they do, not the number, so a change is one edit here.
"""
from __future__ import annotations

TIGHT = 4      # inside a row of related controls
GAP = 6        # between sibling widgets in a panel
GUTTER = 8     # panel edge to content -- the one that was missing
INDENT = 14    # body of a collapsed section, to sit under its arrow


def pad(layout, margin: int = GUTTER, spacing: int = GAP):
    """Apply the standard panel gutter and rhythm to a layout.

    Call this on a panel's root layout rather than setting margins inline, so
    a panel cannot quietly drift away from the others.
    """
    layout.setContentsMargins(margin, margin, margin, margin)
    layout.setSpacing(spacing)
    return layout
