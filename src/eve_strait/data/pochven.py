"""Pochven: Triglavian space, and why it is not part of the map's geometry.

Pochven is 27 systems the Triglavians tore out of empire space in 2020. They
kept their original coordinates -- Niarja still sits on the old Amarr/Jita
corridor -- but nothing about *travel* survived the move:

  * no stargate links Pochven to anything outside it (verified against the
    SDE: zero border gates), and
  * a region-wide cynosural jammer means no cyno can be lit anywhere inside,
    so nothing can *jump in* either.

The jammer only stops a cyno being lit, though, so the border is one-way for
capitals: a capital already stuck inside can still jump *out* to a cyno lit in
normal space. That asymmetry is the same shape as the high-sec rule the router
already models, and it is expressed the same way -- see ``System.jumpable``.

Everything else crosses the border by **filament**, a consumable that is not a
route and cannot be planned as one:

  Inbound   Cladistic (Krai Perun/Svarog/Veles)  -> random system in that clade
            System Class (Border/Internal/Home)  -> random system of that class
  Outbound  Glorification                        -> a fixed "Minor Victory" list
            Proximity                            -> random k-space system
                                                    within PROXIMITY_FILAMENT_LY

Only the outbound Proximity filament depends on where you are, which is why
"fly to the right Pochven system before you pop the filament" is the only part
of filament travel this app can usefully compute.

The tables below are hand-editable on purpose, like ships.py and docking.py.
They only change if CCP redraws Pochven.
"""
from __future__ import annotations

REGION_ID = 10_000_070

# Constellation id -> Triglavian clade. The SDE dump this app downloads has
# constellation *ids* but not their names, so the mapping lives here.
CLADES = {
    20_000_787: "Krai Perun",
    20_000_788: "Krai Svarog",
    20_000_789: "Krai Veles",
}

# The clade "capital" -- the system furthest from that clade's border systems,
# and where Home-class filaments drop you.
HOME_SYSTEMS = {
    "Krai Perun": "Kino",
    "Krai Svarog": "Niarja",
    "Krai Veles": "Archee",
}

# An outbound Proximity filament lands you in a random non-Pochven system
# within this many light years of the Pochven system you left from.
PROXIMITY_FILAMENT_LY = 2.5

# -- inset placement --------------------------------------------------------
# Pochven's real coordinates put it in the middle of empire space, so drawing
# it in place drops 27 unreachable systems across everyone's trade routes. It
# is drawn as an inset instead -- an Alaska box off the side of the map.
#
# The inset is a PURE TRANSLATION at 1:1 scale. No rotation, no rescaling, no
# re-layout. That matters more here than in an atlas: on this map position is
# distance, and distance is jump range, so a schematic layout under the same
# scale bar would invite reading a light-year figure that is pure fiction.
# Translating and nothing else means every distance inside the box is a real
# distance, the scale bar stays valid over it, and the only thing displaced is
# where the region sits -- which the box's own caption says outright.
#
# An earlier version laid the systems out by their gate graph, which drew a
# tidy ring. It was a subway diagram, not an inset: Pochven is really a ~28 ly
# scatter with its three clades interleaved, not a circle. Don't reintroduce
# that without also killing the scale bar over the box.

# Gap in light years between the right edge of New Eden and the inset box, and
# the padding between the box border and the outermost system in it.
INSET_GAP_LY = 14.0
INSET_PAD_LY = 3.5


def inset_offset(kspace_box, pochven_box) -> tuple[float, float]:
    """Translation moving Pochven's real footprint into its inset box.

    Both boxes are ``(min_x, min_y, max_x, max_y)`` in map space -- that is,
    (x, -z), the projection the map draws. Returns (dx, dy) to add to a
    Pochven system's real map-space position.

    Kept here, pure and Qt-free, so the "translation only" property can be
    tested directly: apply this to any two systems and the distance between
    them must be unchanged.
    """
    _, ky0, kx1, ky1 = kspace_box
    px0, py0, _, py1 = pochven_box
    dx = (kx1 + INSET_GAP_LY) - px0
    # Vertically centred on New Eden, so it reads as a sibling of the map
    # rather than something that fell off a corner.
    dy = ((ky0 + ky1) / 2.0 - (py1 - py0) / 2.0) - py0
    return dx, dy
