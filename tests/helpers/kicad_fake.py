"""A stand-in for a running KiCad, speaking the IPC API's own protobuf messages.

KiPI's extractor talks to kipy's real ``Board``. Here the transport is replaced by
:class:`FakeKiCad`, which answers each command the way a given KiCad version does: a
command that version did not have yet is refused with ``AS_UNHANDLED``, as KiCad does
for any request it has no handler for. kipy's own parsing therefore runs unchanged, so a
kicad-python release that changes what it reads or returns is caught too.

The board is a 4-layer test board (all coordinates in mm):

* ``+3V3``: a 10 mm, 0.5 mm wide track on F.Cu ending in an arc, a through via at
  (10, 0), and pad U1.1 (SMD, F.Cu only).
* ``GND``: pad U1.2, a plated slot (2.0 x 0.8 mm drill, pad turned 90 degrees), and a
  filled zone on In1.Cu. In1.Cu is named "GND_PLANE" by the user.
* A rule area (not copper), and a 30 x 20 mm Edge.Cuts outline.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from google.protobuf.any_pb2 import Any as AnyMsg
from google.protobuf.empty_pb2 import Empty

from kipy.board import Board
from kipy.errors import ApiError
from kipy.proto.board import board_commands_pb2 as bc
from kipy.proto.board import board_pb2 as bp
from kipy.proto.board import board_types_pb2 as bt
from kipy.proto.board.board_types_pb2 import BoardLayer as L
from kipy.proto.common.commands import editor_commands_pb2 as ec
from kipy.proto.common.envelope_pb2 import ApiStatusCode
from kipy.proto.common.types import DocumentSpecifier, KiCadObjectType as T

NM = 1_000_000
COPPER = [L.BL_F_Cu, L.BL_In1_Cu, L.BL_In2_Cu, L.BL_B_Cu]
NAMES = {L.BL_F_Cu: "F.Cu", L.BL_In1_Cu: "GND_PLANE", L.BL_In2_Cu: "In2.Cu",
         L.BL_B_Cu: "B.Cu"}
CU_UM = {L.BL_F_Cu: 35, L.BL_In1_Cu: 17.5, L.BL_In2_Cu: 17.5, L.BL_B_Cu: 35}
DIELECTRICS = [("PP 2116", 4.3, 0.2), ("Core FR4", 4.6, 1.065), ("PP 2116", 4.3, 0.2)]

# The KiCad release that added each command KiPI uses (older ones have no handler).
ADDED = {
    "GetNetClassForNets": (9, 0, 1),
    "CheckPadstackPresenceOnLayers": (9, 0, 3),
    "GetBoardEnabledLayers": (9, 0, 5),
    "GetBoardLayerName": (9, 0, 8),
}
GROUPS_ADDED = (10, 0, 0)          # KOT_PCB_GROUP in GetItems

# Versions worth testing: each is the first with something KiPI uses, the latest, and
# 9.0.7 (the last without GetBoardLayerName, as reported by a user).
VERSIONS = ["9.0.0", "9.0.1", "9.0.3", "9.0.5", "9.0.7", "9.0.8", "10.0.0", "10.0.1", "10.0.6"]


def parse(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


# ------------------------------------------------------------------ protobuf builders

def _v(m, x, y):
    m.x_nm, m.y_nm = round(x * NM), round(y * NM)


def _polyline(m, pts):
    for x, y in pts:
        _v(m.nodes.add().point, x, y)
    m.closed = True


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _pack(msg) -> AnyMsg:
    a = AnyMsg()
    a.Pack(msg)
    return a


def _net(m, name):
    m.name = name
    m.code.value = {"+3V3": 1, "GND": 2}.get(name, 0)


class FakeBoardData:
    """The board's items as KiCad would serialize them."""

    def __init__(self):
        self.tracks: List[AnyMsg] = []
        self.vias: List[AnyMsg] = []
        self.footprints: List[AnyMsg] = []
        self.zones: List[AnyMsg] = []
        self.shapes: List[AnyMsg] = []
        self.pad_shapes: Dict[str, List] = {}     # pad id -> [(layers, outline)]
        self.pad_present: Dict[str, List] = {}    # pad id -> layers with copper
        self._build()

    def _build(self):
        t = bt.Track()
        t.id.value = "trk-1"
        _v(t.start, 0, 0)
        _v(t.end, 10, 0)
        t.width.value_nm = int(0.5 * NM)
        t.layer = L.BL_F_Cu
        _net(t.net, "+3V3")
        self.tracks.append(_pack(t))

        a = bt.Arc()
        a.id.value = "arc-1"
        _v(a.start, 0, 0)
        _v(a.mid, -2, 2)
        _v(a.end, 0, 4)
        a.width.value_nm = int(0.5 * NM)
        a.layer = L.BL_F_Cu
        _net(a.net, "+3V3")
        self.tracks.append(_pack(a))

        v = bt.Via()
        v.id.value = "via-1"
        _v(v.position, 10, 0)
        v.type = bt.ViaType.VT_THROUGH
        _net(v.net, "+3V3")
        ps = v.pad_stack
        ps.type = bt.PadStackType.PST_NORMAL
        ps.layers.extend(COPPER)
        ps.drill.start_layer, ps.drill.end_layer = L.BL_F_Cu, L.BL_B_Cu
        _v(ps.drill.diameter, 0.3, 0.3)
        cl = ps.copper_layers.add()
        cl.layer = L.BL_F_Cu
        _v(cl.size, 0.6, 0.6)
        self.vias.append(_pack(v))

        fp = bt.FootprintInstance()
        fp.id.value = "fp-u1"
        _v(fp.position, 20, 5)
        fp.layer = L.BL_F_Cu
        fp.reference_field.text.text.text = "U1"
        fp.value_field.text.text.text = "TPS62130"
        smd = bt.Pad()
        smd.id.value = "pad-u1-1"
        smd.number = "1"
        smd.type = bt.PadType.PT_SMD
        _net(smd.net, "+3V3")
        _v(smd.position, 19, 5)
        smd.pad_stack.layers.append(L.BL_F_Cu)
        self.pad_shapes["pad-u1-1"] = [([L.BL_F_Cu], _rect(18.5, 4.5, 19.5, 5.5))]
        self.pad_present["pad-u1-1"] = [L.BL_F_Cu]
        pth = bt.Pad()
        pth.id.value = "pad-u1-2"
        pth.number = "2"
        pth.type = bt.PadType.PT_PTH
        _net(pth.net, "GND")
        _v(pth.position, 22, 5)
        pth.pad_stack.layers.extend(COPPER)
        pth.pad_stack.angle.value_degrees = 90.0
        _v(pth.pad_stack.drill.diameter, 2.0, 0.8)   # in the pad's own frame
        # Turned 90 degrees: 1.7 wide, 2.9 tall on the board. KiCad returns the outline
        # on every layer; "remove unconnected" leaves copper only on the outer ones.
        self.pad_shapes["pad-u1-2"] = [(COPPER, _rect(21.15, 3.55, 22.85, 6.45))]
        self.pad_present["pad-u1-2"] = [L.BL_F_Cu, L.BL_B_Cu]
        fp.definition.items.extend([_pack(smd), _pack(pth)])
        self.footprints.append(_pack(fp))

        z = bt.Zone()
        z.id.value = "zone-gnd"
        z.type = bt.ZoneType.ZT_COPPER
        z.name = "GND pour"
        z.priority = 1
        z.layers.append(L.BL_In1_Cu)
        _net(z.copper_settings.net, "GND")
        fill = z.filled_polygons.add()
        fill.layer = L.BL_In1_Cu
        _polyline(fill.shapes.polygons.add().outline, _rect(1, -4, 29, 14))
        z.filled = True
        self.zones.append(_pack(z))

        keepout = bt.Zone()
        keepout.id.value = "zone-keepout"
        keepout.type = bt.ZoneType.ZT_RULE_AREA
        keepout.layers.append(L.BL_F_Cu)
        self.zones.append(_pack(keepout))

        corners = _rect(0, -5, 30, 15)
        for i, (x0, y0) in enumerate(corners):
            x1, y1 = corners[(i + 1) % 4]
            s = bt.BoardGraphicShape()
            s.id.value = f"edge-{i}"
            s.layer = L.BL_Edge_Cuts
            _v(s.shape.segment.start, x0, y0)
            _v(s.shape.segment.end, x1, y1)
            self.shapes.append(_pack(s))


class FakeKiCad:
    """kipy's client, replaced: answers commands as KiCad ``version`` would."""

    def __init__(self, version: str = "10.0.6"):
        self.version = parse(version)
        self.data = FakeBoardData()
        self.busy_polls = 0          # GetItems calls answered AS_BUSY after a refill
        self.calls: List[str] = []

    def board(self) -> Board:
        doc = DocumentSpecifier()
        doc.board_filename = "test.kicad_pcb"
        doc.project.path = "/projects/test"
        return Board(self, doc)

    # kipy's Board only ever calls ``send``.
    def send(self, cmd, response_type):
        name = type(cmd).__name__
        self.calls.append(name)
        if self.version < ADDED.get(name, (0,)):
            # Exactly what kipy raises for KiCad's reply (KICAD_API_SERVER::handleApiEvent).
            raise ApiError("KiCad returned error: no handler available for request of type "
                           f"{cmd.DESCRIPTOR.full_name}", code=ApiStatusCode.AS_UNHANDLED)
        handler = getattr(self, "_" + name, None)
        if handler is None:
            raise NotImplementedError(f"fake KiCad does not answer {name}")
        return handler(cmd)

    def _RefillZones(self, cmd):
        self.busy_polls = 2
        return Empty()

    def _GetItems(self, cmd):
        if self.busy_polls:
            self.busy_polls -= 1
            raise ApiError("KiCad is busy", code=ApiStatusCode.AS_BUSY)
        groups = {T.KOT_PCB_TRACE: self.data.tracks, T.KOT_PCB_ARC: [],
                  T.KOT_PCB_VIA: self.data.vias, T.KOT_PCB_FOOTPRINT: self.data.footprints,
                  T.KOT_PCB_ZONE: self.data.zones, T.KOT_PCB_SHAPE: self.data.shapes,
                  T.KOT_PCB_TEXT: [], T.KOT_PCB_TEXTBOX: []}
        if self.version >= GROUPS_ADDED:
            groups[T.KOT_PCB_GROUP] = []
        known = [t for t in cmd.types if t in groups]
        if not known:
            raise ApiError("none of the requested types are valid for a Board object",
                           code=ApiStatusCode.AS_BAD_REQUEST)
        r = ec.GetItemsResponse()
        for t in known:
            r.items.extend(groups[t])
        return r

    def _GetBoardStackup(self, cmd):
        r = bc.BoardStackupResponse()
        for i, lid in enumerate(COPPER):
            sl = r.stackup.layers.add()
            sl.type, sl.layer, sl.enabled = bp.BoardStackupLayerType.BSLT_COPPER, lid, True
            sl.thickness.value_nm = int(CU_UM[lid] * 1000)
            sl.user_name = NAMES[lid]
            if i < len(DIELECTRICS):
                mat, er, t_mm = DIELECTRICS[i]
                d = r.stackup.layers.add()
                d.type, d.enabled = bp.BoardStackupLayerType.BSLT_DIELECTRIC, True
                d.thickness.value_nm = int(t_mm * NM)
                sub = d.dielectric.layer.add()
                sub.material_name, sub.epsilon_r = mat, er
                sub.thickness.value_nm = int(t_mm * NM)
        return r

    def _GetBoardEnabledLayers(self, cmd):
        r = bc.BoardEnabledLayersResponse()
        r.copper_layer_count = len(COPPER)
        r.layers.extend(COPPER + [L.BL_F_SilkS, L.BL_Edge_Cuts])
        return r

    def _GetBoardLayerName(self, cmd):
        r = bc.BoardLayerNameResponse()
        r.name = NAMES.get(cmd.layer, "")
        return r

    def _GetNets(self, cmd):
        r = bc.NetsResponse()
        for name in ("+3V3", "GND"):
            _net(r.nets.add(), name)
        return r

    def _GetNetClassForNets(self, cmd):
        r = bc.NetClassForNetsResponse()
        for n in cmd.net:
            r.classes[n.name].name = "Power" if n.name == "+3V3" else "Default"
        return r

    def _CheckPadstackPresenceOnLayers(self, cmd):
        r = bc.PadstackPresenceResponse()
        for item in cmd.items:
            for lid in cmd.layers:
                e = r.entries.add()
                e.item.value, e.layer = item.value, lid
                e.presence = (bc.PadstackPresence.PSP_PRESENT
                              if lid in self.data.pad_present.get(item.value, [])
                              else bc.PadstackPresence.PSP_NOT_PRESENT)
        return r

    def _GetPadShapeAsPolygon(self, cmd):
        r = bc.PadShapeAsPolygonResponse()
        for pid in cmd.pads:
            r.pads.add().value = pid.value
            poly = r.polygons.add()
            for layers, outline in self.data.pad_shapes.get(pid.value, []):
                if cmd.layer in layers:
                    _polyline(poly.outline, outline)
        return r
