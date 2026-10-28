"""Toolkit-independent snapshot of the board data KiPI needs.

Every board source (the running KiCad, a board file) produces one, and the rest of KiPI
works from it alone. Everything is in millimeters, with board coordinates as KiCad uses
them (x right, y down). Z is up, with z = 0 at the bottom surface of the board. Copper
layers are numbered as in :mod:`kipi.core.model.layers`. Polygons are stored as plain
coordinate lists so a snapshot can be saved to JSON and reloaded without KiCad.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

Point = Tuple[float, float]
Ring = List[Point]


#: What each ``BoardSnapshot.source_format`` is called in the UI.
FORMAT_NAMES: Dict[str, str] = {
    "kicad-ipc": "KiCad PCB editor",
    "kicad-pcb": "KiCad board file",
    "odbpp": "ODB++",
    "hyp": "HyperLynx",
}


@dataclass
class Poly:
    """Polygon with holes. Rings are implicitly closed."""
    outline: Ring
    holes: List[Ring] = field(default_factory=list)


@dataclass
class CopperLayer:
    id: int                 # KiCad BoardLayer enum value
    name: str
    index: int              # 0 = top copper, increasing downward
    thickness_mm: float
    z_bottom_mm: float      # bottom face of the copper foil

    @property
    def z_top_mm(self) -> float:
        return self.z_bottom_mm + self.thickness_mm

    @property
    def z_mid_mm(self) -> float:
        return self.z_bottom_mm + 0.5 * self.thickness_mm


@dataclass
class DielectricLayer:
    thickness_mm: float
    material: str = ""
    epsilon_r: float = 0.0
    above_layer: int = -1   # copper layer id directly above
    below_layer: int = -1   # copper layer id directly below
    # In-plane thermal conductivity, W/(m K), set in the Stackup window; 0 = the typical
    # value for its material (kipi.dc.thermal.materials).
    k_w_mk: float = 0.0


#: Coating kinds, outermost last: a solder mask over the copper, a silkscreen over that.
MASK, SILK = "mask", "silk"


@dataclass
class Coating:
    """A solder mask or silkscreen over an outer copper layer (#82). Coatings sit outside
    the board: they do not count in its thickness or move any z."""
    kind: str               # MASK or SILK
    side: str               # "top" or "bottom"
    thickness_mm: float
    material: str = ""
    color: str = ""         # "#rrggbb", "" when the source does not say

    @property
    def name(self) -> str:
        what = "Solder mask" if self.kind == MASK else "Silkscreen"
        return f"{'Top' if self.side == 'top' else 'Bottom'} {what.lower()}"


@dataclass
class Stackup:
    copper: List[CopperLayer]
    dielectrics: List[DielectricLayer]
    board_thickness_mm: float
    from_board: bool = True  # False when a default stackup had to be synthesized
    edited: bool = False     # thicknesses changed in KiPI (see kipi.core.model.stackup)
    # Solder masks and silkscreens, top to bottom: the top silkscreen first, then the top
    # mask, the bottom mask and the bottom silkscreen; any of them may be missing.
    coatings: List[Coating] = field(default_factory=list)

    def __setstate__(self, state):      # results saved before #82 have no coatings
        self.__dict__.update(state)
        self.__dict__.setdefault("coatings", [])

    def coatings_on(self, side: str) -> List[Coating]:
        """The coatings on ``side`` ("top" or "bottom"), from the copper outward."""
        out = [c for c in self.coatings if c.side == side]
        return out[::-1] if side == "top" else out

    def layer(self, layer_id: int) -> Optional[CopperLayer]:
        for layer in self.copper:
            if layer.id == layer_id:
                return layer
        return None

    def layers_between(self, a: int, b: int) -> List[int]:
        """Copper layer ids from a to b inclusive, in stack order."""
        ids = [layer.id for layer in self.copper]
        if a not in ids or b not in ids:
            return []
        ia, ib = sorted((ids.index(a), ids.index(b)))
        return ids[ia:ib + 1]


@dataclass
class Net:
    code: int
    name: str
    netclass: str = ""


@dataclass
class Track:
    net: str
    layer: int
    width_mm: float
    start: Point
    end: Point
    mid: Optional[Point] = None  # set for arc tracks (start/mid/end arc)
    id: str = ""                 # the editor's id, where the board came from one

    @property
    def is_arc(self) -> bool:
        return self.mid is not None


@dataclass
class Via:
    id: str
    net: str
    position: Point
    drill_mm: float
    diameter_mm: float
    layers: List[int]       # copper layers spanned, top to bottom
    via_type: str = "through"
    filled: Optional[bool] = None    # KiCad's drill filling; None when it does not say
    # Pad diameter per copper layer. A spanned layer missing here has no pad (unused
    # pads removed); empty (older snapshots) means diameter_mm on every spanned layer.
    pad_mm: Dict[int, float] = field(default_factory=dict)

    def pad_diameter(self, layer: int) -> float:
        if not self.pad_mm:
            return self.diameter_mm if layer in self.layers else 0.0
        return self.pad_mm.get(layer, 0.0)


@dataclass
class Pad:
    id: str
    net: str
    footprint: str          # reference designator, "" if unknown
    number: str
    position: Point
    pad_type: str           # "pth", "smd", "npth", "edge", "unknown"
    drill_mm: Tuple[float, float] = (0.0, 0.0)   # x, y in the pad's own frame (unrotated)
    shapes: Dict[int, List[Poly]] = field(default_factory=dict)  # layer id -> copper shape
    # Pad orientation on the board, degrees counter-clockwise as KiCad shows it (footprint
    # rotation included). None in snapshots saved before it was recorded.
    angle_deg: Optional[float] = None

    @property
    def layers(self) -> List[int]:
        return sorted(self.shapes.keys())

    @property
    def label(self) -> str:
        return f"{self.footprint}.{self.number}" if self.footprint else self.number


@dataclass
class Footprint:
    id: str
    reference: str
    value: str
    position: Point
    side: str               # "top" or "bottom"
    pad_ids: List[str] = field(default_factory=list)
    footprint: str = ""     # library footprint, e.g. "Capacitor_SMD:C_0402_1005Metric"
    # Rotation on the board, degrees counter-clockwise as KiCad shows it. None in
    # snapshots saved before it was recorded.
    angle_deg: Optional[float] = None


@dataclass
class Zone:
    id: str
    net: str
    name: str
    priority: int
    filled: Dict[int, List[Poly]] = field(default_factory=dict)


@dataclass
class BoardSnapshot:
    board_name: str
    project_dir: str
    stackup: Stackup
    nets: List[Net]
    tracks: List[Track]
    vias: List[Via]
    pads: List[Pad]
    footprints: List[Footprint]
    zones: List[Zone]
    outline: List[Poly]
    source: str = "kicad"   # free text naming where the board came from, for the UI
    kicad_version: str = ""
    # The format the board was first read from ("kicad-ipc"; later "kicad-pcb", "hyp",
    # "odbpp", "cce"), kept when the board is saved as a snapshot. "" if unknown.
    source_format: str = ""
    # What the source did not give and KiPI filled in with a placeholder ("Stackup: the
    # board's own is incomplete, ..."), one sentence each, shown with the board.
    assumed: List[str] = field(default_factory=list)
    # The snapshot file this was read from; "" when read from the board's own source.
    # Never saved into a snapshot.
    file: str = ""
    # What the silkscreens print (#82), by side ("top", "bottom"): graphics and text as
    # polygons. Empty for a side with nothing printed, and in snapshots from before it.
    silkscreen: Dict[str, List[Poly]] = field(default_factory=dict)

    def __setstate__(self, state):      # results saved before the silkscreen was read
        self.__dict__.update(state)
        self.__dict__.setdefault("silkscreen", {})

    @property
    def format_name(self) -> str:
        """What the board was read from, for the UI: the format's name, else whatever the
        board says of itself (a generated board names its script)."""
        return (FORMAT_NAMES.get(self.source_format) or self.source_format or self.source
                or "unknown source")

    def net_names(self) -> List[str]:
        return [n.name for n in self.nets if n.name]

    def items_on_net(self, net: str):
        """``(tracks, vias, pads, zones)`` on ``net``, from an index built on first use."""
        index = self.__dict__.get("_net_index")
        if index is None:
            index = {}
            for kind, items in enumerate((self.tracks, self.vias, self.pads, self.zones)):
                for item in items:
                    index.setdefault(item.net, ([], [], [], []))[kind].append(item)
            # Not a dataclass field, so snapshots never serialize it.
            self.__dict__["_net_index"] = index
        return index.get(net, ([], [], [], []))

    def invalidate_index(self):
        """Call after editing the item lists in place."""
        for key in ("_net_index", "_ref_index", "_biggest_net", "_net_kinds", "_drill_holes"):
            self.__dict__.pop(key, None)
