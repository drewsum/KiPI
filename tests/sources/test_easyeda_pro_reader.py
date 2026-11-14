"""The EasyEDA Pro .epro2 reader: document replay, geometry and KiPI snapshot."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from kipi.core.model.snapshot_io import load as load_snapshot
from kipi.core.model.snapshot_io import save as save_snapshot
from kipi.sources import registry
from kipi.sources.easyeda_pro.archive import EasyedaError, open_project
from kipi.sources.easyeda_pro.documents import (
    Document, parse_stream, split_record,
)
from kipi.sources.easyeda_pro.primitives import (
    MAX_ERROR_MM, Units, choose_units, flatten_path, pad_shape_for_layer,
    place_in_component, to_ring,
)
from kipi.sources.easyeda_pro.reader import read_board

DATA = Path(__file__).parents[1] / "data" / "easyeda" / "features.epro2"


@pytest.fixture(scope="module")
def snap():
    return read_board(DATA)


def test_identity(snap):
    assert snap.board_name == "features"
    assert snap.project_dir == str(DATA.parent)
    assert snap.source_format == "easyeda-epro2"
    assert snap.format_name == "EasyEDA Pro (epro2)"


def test_stackup(snap):
    copper = snap.stackup.copper
    assert [c.id for c in copper] == [3, 4, 5, 34]
    assert [c.name for c in copper] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert [round(c.thickness_mm, 4) for c in copper] == [0.035, 0.0152, 0.0152, 0.035]
    assert round(snap.stackup.board_thickness_mm, 4) == 1.5862
    assert [d.material for d in snap.stackup.dielectrics][1] == "1.1mm H/HOZ Copper"
    assert all(d.epsilon_r == 0 for d in snap.stackup.dielectrics)


def test_nets_and_tracks(snap):
    assert {n.name for n in snap.nets} == {"", "GND", "VIN", "VOUT"}
    assert len(snap.tracks) == 4
    line = next(t for t in snap.tracks if t.start == (11.43, -10.16))
    assert (line.net, line.layer, line.width_mm) == ("GND", 3, 0.254)
    assert line.end == (13.97, -10.16)
    arc = next(t for t in snap.tracks if t.mid is not None)
    assert arc.layer == 5 and arc.net == "GND"
    # The same id was written as a LINE and then as a VIA; the later type wins.
    assert not any(t.id == "dup" for t in snap.tracks)


def test_vias(snap):
    assert len(snap.vias) == 3
    assert all(v.via_type == "through" for v in snap.vias)
    assert {v.net for v in snap.vias} == {"VIN", "GND"}
    assert all(v.layers == [3, 4, 5, 34] for v in snap.vias)
    assert round(snap.vias[0].drill_mm, 4) == 0.305
    assert round(snap.vias[0].diameter_mm, 4) == 0.61
    assert any("blind" in note.lower() for note in snap.assumed)


def test_pads_and_footprints(snap):
    assert len(snap.pads) == 10
    assert len(snap.footprints) == 2
    by_id = {p.id: p for p in snap.pads}
    assert by_id["U1e1"].net == "VIN"
    assert by_id["U1e1"].shapes.keys() == {3}
    assert by_id["U1e4"].pad_type == "pth"
    assert by_id["U1e4"].shapes.keys() == {3, 4, 5, 34}
    # specialPad replaces the default ellipse on Inner1 only.
    assert len(by_id["U1e4"].shapes[4][0].outline) == 4
    assert len(by_id["U1e4"].shapes[3][0].outline) > 4
    assert by_id["U1e5"].pad_type == "npth"
    assert by_id["U1e5"].drill_mm == pytest.approx((0.3048, 0.1524), abs=1e-6)
    # The bottom-side part's local top pads land on B.Cu.
    assert by_id["C1e1"].net == "VOUT"
    assert by_id["C1e1"].shapes.keys() == {34}
    bottom = next(f for f in snap.footprints if f.reference == "C1")
    assert bottom.side == "bottom"
    assert bottom.footprint == "TEST:FP8"
    top = next(f for f in snap.footprints if f.reference == "U1")
    assert top.side == "top" and top.value == "U1"


def test_tombstoned_pad_net_is_gone(snap):
    # The fixture writes and then deletes a TEMPLATE pad mapping; it must not create a pad.
    assert all("TEMPLATE" not in p.footprint for p in snap.pads)


def test_zones_and_outline(snap):
    assert len(snap.zones) == 2
    vin = next(z for z in snap.zones if z.net == "VIN")
    assert list(vin.filled) == [3]
    bounds = Polygon(vin.filled[3][0].outline).bounds
    assert bounds == pytest.approx((2.54, -17.78, 10.16, -15.24), abs=1e-3)
    vout = next(z for z in snap.zones if z.net == "VOUT")
    assert list(vout.filled) == [34]
    assert len(snap.outline) == 1
    assert Polygon(snap.outline[0].outline).bounds == pytest.approx(
        (0.0, -20.32, 25.4, 0.0), abs=1e-3)


def test_malformed_line_is_reported(snap):
    assert any("malformed" in note for note in snap.assumed)


def test_registry():
    fmt = registry.format_for(DATA)
    assert fmt is not None and fmt.name == "EasyEDA Pro (epro2)"
    source = registry.open_board(DATA)
    assert source.caps.reload and source.files() == [DATA]
    loaded = source.load()
    assert loaded.source_format == "easyeda-epro2"


def test_snapshot_roundtrip(snap, tmp_path):
    path = tmp_path / "board.kipi.json.gz"
    save_snapshot(snap, path)
    loaded = load_snapshot(path)
    assert loaded.source_format == "easyeda-epro2"
    assert loaded.board_name == snap.board_name
    assert len(loaded.tracks) == len(snap.tracks)
    assert len(loaded.vias) == len(snap.vias)
    assert len(loaded.pads) == len(snap.pads)
    assert len(loaded.zones) == len(snap.zones)
    assert [c.id for c in loaded.stackup.copper] == [3, 4, 5, 34]


def test_errors(tmp_path):
    plain = tmp_path / "board.epro2"
    plain.write_text("not a zip", encoding="utf-8")
    with pytest.raises(EasyedaError, match="not a ZIP"):
        open_project(plain)

    empty = tmp_path / "empty.epro2"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("project2.json", "{}")
    with pytest.raises(EasyedaError, match="no .epru"):
        open_project(empty)

    older = tmp_path / "older.epro2"
    with zipfile.ZipFile(older, "w") as archive:
        archive.writestr("PCB/1.epcb", "[]")
    with pytest.raises(EasyedaError, match="older EasyEDA Pro"):
        open_project(older)


def test_no_pcb_document(monkeypatch):
    from kipi.sources.easyeda_pro import documents as documents_module

    docs, errors = parse_stream(
        '{"type":"DOCHEAD"}||{"docType":"FOOTPRINT","uuid":"fp1"}|'
        '{"type":"PAD","ticket":1,"id":"e1"}||{"num":"1"}|')
    assert not errors
    with pytest.raises(EasyedaError, match="no PCB document"):
        documents_module.pick_pcb(docs)


def test_split_record_handles_separator_inside_strings():
    line = '{"type":"POLY","id":"x","ticket":1}||{"name":"a||b"}|'
    outer, inner = split_record(line)
    assert outer["id"] == "x" and inner["name"] == "a||b"
    assert split_record('{"type":"X","id":"x","ticket":1}|||')[1] is None
    assert split_record('{"type":"X","id":"x","ticket":1}||""|')[1] is None


def test_replay_ticket_and_client_tie_break():
    doc = Document(head={"docType": "PCB", "uuid": "p", "client": "b"})
    doc.add({"type": "LINE", "id": "x", "ticket": 2}, {"name": "old"}, "b")
    doc.add({"type": "LINE", "id": "x", "ticket": 1}, {"name": "lower"}, "z")
    assert doc.of_type("LINE")[0]["name"] == "old"      # larger ticket wins
    doc.add({"type": "LINE", "id": "x", "ticket": 2}, {"name": "client-b"}, "b")
    doc.add({"type": "LINE", "id": "x", "ticket": 2}, {"name": "client-c"}, "c")
    assert doc.of_type("LINE")[0]["name"] == "client-c"   # larger client wins


def test_replay_type_change_and_delete_resurrect():
    doc = Document(head={"docType": "PCB", "uuid": "p", "client": "a"})
    doc.add({"type": "LINE", "id": "x", "ticket": 1}, {"a": 1})
    doc.add({"type": "VIA", "id": "x", "ticket": 2}, {"b": 2})
    assert doc.of_type("LINE") == [] and len(doc.of_type("VIA")) == 1
    doc.add({"type": "VIA", "id": "x", "ticket": 3}, None)
    assert doc.of_type("VIA") == []
    doc.add({"type": "VIA", "id": "x", "ticket": 4}, {"c": 3})
    assert len(doc.of_type("VIA")) == 1


def test_primitives_unit_and_paths():
    project = {"editorVersion": "4.1.99"}
    docs, _ = parse_stream('{"type":"DOCHEAD"}||{"docType":"PCB","uuid":"p"}|'
                           '{"type":"VIA","ticket":1,"id":"v"}||'
                           '{"holeDiameter":12.0078}|')
    units = choose_units(project, docs[0], [])
    assert units.mm == 0.0254
    assert units.poured_mm == 0.254
    ring = to_ring(flatten_path(["R", 0, 800, 1000, 800, 0, 100]), units)
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    assert (min(xs), min(ys), max(xs), max(ys)) == pytest.approx(
        (0.0, -20.32, 25.4, 0.0), abs=1e-6)
    line = flatten_path([0, 0, "L", 100, 0, 100, 100])
    assert line == [(0, 0), (100, 0), (100, 100)]


def test_pad_shapes_and_component_transform():
    pad = {"centerX": 59.74, "centerY": 0, "padAngle": 0,
           "defaultPad": {"padType": "RECT", "width": 64.37, "height": 106.299,
                          "radius": 0}}
    rings, absolute = pad_shape_for_layer(pad, 1, MAX_ERROR_MM / 0.0254)
    assert not absolute and len(rings[0]) == 4
    placed = place_in_component(
        [[(59.74, 0)]], {"x": 1574, "y": 1701, "angle": 90}, "top",
        Units(mm=0.0254, thickness_mm=0.0254))
    assert placed[0][0] == pytest.approx((39.9796, -44.7228), abs=1e-4)


def test_poured_scale():
    snap = read_board(DATA)
    vin = next(z for z in snap.zones if z.net == "VIN")
    # POUR boundary is 100..400 mil x 600..700; POURED is 10..40 x 60..70 in
    # 0.01-inch units. Both must land on the same rectangle.
    assert Polygon(vin.filled[3][0].outline).bounds == pytest.approx(
        (2.54, -17.78, 10.16, -15.24), abs=1e-3)


@pytest.mark.parametrize("angle,top_center,bottom_center", [
    (0, (220, 330), (220, 270)), (90, (170, 320), (230, 320)),
    (180, (180, 270), (180, 330)), (270, (230, 280), (170, 280)),
])
@pytest.mark.parametrize("origin", [(0, 0), (40, -50)])
@pytest.mark.parametrize("side", ["top", "bottom"])
def test_placed_pad_connects_to_board_copper(
        tmp_path, angle, top_center, bottom_center, origin, side):
    """Respect the footprint origin and mirror local Y before bottom rotation."""
    from kipi.core.geometry.copper import net_layer_copper
    from kipi.core.model.layers import B_CU, F_CU

    def record(kind, rid, body):
        return (json.dumps({"type": kind, "id": rid, "ticket": 1}) + "||"
                + json.dumps(body) + "|\n")

    x, y = bottom_center if side == "bottom" else top_center
    source_layer = 2 if side == "bottom" else 1
    copper_layer = B_CU if side == "bottom" else F_CU
    stream = "".join([
        record("DOCHEAD", "fp", {"docType": "FOOTPRINT", "uuid": "fp"}),
        record("CANVAS", "CANVAS", {"originX": origin[0], "originY": origin[1]}),
        record("PAD", "p1", {
            "num": "1", "layerId": 1,
            "centerX": 20 + origin[0], "centerY": 30 + origin[1],
            "padAngle": 30,
            "defaultPad": {"padType": "RECT", "width": 10, "height": 20},
        }),
        record("DOCHEAD", "pcb", {"docType": "PCB", "uuid": "pcb"}),
        record("LAYER", "top", {"layerId": 1, "layerType": "TOP"}),
        record("LAYER", "bottom", {"layerId": 2, "layerType": "BOTTOM"}),
        record("COMPONENT", "c1", {
            "layerId": source_layer, "x": 200, "y": 300, "angle": angle,
        }),
        record("ATTR", "fpattr", {
            "parentId": "c1", "key": "Footprint", "value": "fp",
        }),
        record("PAD_NET", '["PAD_NET","c1","1","p1"]', {"padNet": "VCC"}),
        record("LINE", "wire", {
            "netName": "VCC", "layerId": source_layer, "width": 4,
            "startX": x, "startY": y, "endX": x + 50, "endY": y,
        }),
    ])
    path = tmp_path / "placed.epro2"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("project2.json", '{"editorVersion":"4.1.99"}')
        archive.writestr("placed.epru", stream)
    board = read_board(path)
    pad = board.pads[0]
    assert pad.position == pytest.approx((x * 0.0254, -y * 0.0254), abs=1e-6)
    assert pad.net == "VCC" and pad.shapes.keys() == {copper_layer}
    # Displaced pads become separate copper islands, and cannot draw current.
    assert net_layer_copper(board, "VCC", copper_layer).geom_type == "Polygon"
