"""Build the small synthetic EasyEDA Pro project used by the reader tests.

The real project used to develop the reader is the user's own board and is not committed.
This fixture keeps a tiny but complete document stream: a four-layer stackup, embedded
footprints, pads with every shape, nets, tracks, vias, pours and an outline.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _line(kind, payload, ticket=1, record_id=None):
    head = {"type": kind, "ticket": ticket}
    if record_id is not None:
        head["id"] = record_id
    return json.dumps(head, ensure_ascii=False, separators=(",", ":")) + "||" + \
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "|\n"


def _delete(kind, record_id, ticket=1):
    head = {"type": kind, "ticket": ticket, "id": record_id}
    return json.dumps(head, ensure_ascii=False, separators=(",", ":")) + "|||\n"


def _dochead(kind, uuid, client="clientA", version="4.1.99"):
    head = {"type": "DOCHEAD"}
    body = {"docType": kind, "uuid": uuid, "client": client, "editVersion": version}
    return json.dumps(head, ensure_ascii=False, separators=(",", ":")) + "||" + \
        json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "|\n"


def _layer(layer_id, layer_type, name, ticket):
    return _line("LAYER", {
        "layerId": layer_id, "layerType": layer_type, "layerName": name,
        "use": True, "show": True, "locked": False,
        "activeColor": "#ff0000", "activateTransparency": 1,
        "inactiveColor": "#7f0000", "inactiveTransparency": 0.5,
    }, ticket, f'["LAYER",{layer_id}]')


def _phys(layer_id, material, thickness, er, z, ticket):
    return _line("LAYER_PHYS", {
        "material": material, "thickness": thickness, "permittivity": er,
        "lossTangent": 0, "isKeepIsland": True, "zIndex": z,
    }, ticket, f'["LAYER_PHYS",{layer_id}]')


def _pad(num, x, y, shape, layer_id=1, hole=None, plated=True, pad_angle=0,
         special=None, ticket=1):
    body = {
        "groupId": 0, "netName": "", "layerId": layer_id, "num": num,
        "centerX": x, "centerY": y, "padAngle": pad_angle, "hole": hole,
        "defaultPad": shape, "specialPad": special or [], "padOffsetX": 0,
        "padOffsetY": 0, "relativeAngle": 0, "plated": plated, "padType": "NORMAL",
        "topSolderExpansion": 2, "bottomSolderExpansion": 2,
        "topPasteExpansion": 0, "bottomPasteExpansion": 0,
        "locked": False, "zIndex": 1, "connectMode": None,
    }
    return _line("PAD", body, ticket, f"e{num}")


def _component(component_id, x, y, angle, layer_id, name, ticket):
    return _line("COMPONENT", {
        "partitionId": "", "groupId": 0, "layerId": layer_id, "x": x, "y": y,
        "angle": angle, "attrs": {"Name": name}, "locked": False, "zIndex": -1,
    }, ticket, component_id)


def _attr(parent, key, value, ticket):
    return _line("ATTR", {
        "parentId": parent, "groupID": 0, "layerId": 3, "x": None, "y": None,
        "key": key, "value": value, "keyVisible": False, "valueVisible": False,
        "fontFamily": "default", "fontSize": 45, "strokeWidth": 6, "bold": 0,
        "italic": 0, "origin": "LEFT_BOTTOM", "angle": 0, "reverse": False,
        "expansion": 0, "mirror": False, "locked": False, "zIndex": -1,
        "specialColor": None,
    }, ticket, f"{parent}{key}")


def _net(name, ticket):
    return _line("NET", {
        "netType": None, "specialColor": None, "retLine": True, "differentialName": None,
        "isPositiveNet": False, "equalLengthGroupName": None,
    }, ticket, f'["NET","{name}"]')


def _pad_net(component, number, pad_id, net, ticket):
    return _line("PAD_NET", {"partitionId": "", "padNet": net, "padLen": None,
                             "propagationDelay": None, "attrsMap": {}},
                 ticket, f'["PAD_NET","{component}","{number}","{pad_id}"]')


def _line_seg(net, layer, x1, y1, x2, y2, width, ticket, rid):
    return _line("LINE", {
        "partitionId": "", "groupId": 0, "netName": net, "layerId": layer,
        "startX": x1, "startY": y1, "endX": x2, "endY": y2, "width": width,
        "locked": False, "zIndex": -1,
    }, ticket, rid)


def _via(net, x, y, kind, ticket, rid):
    return _line("VIA", {
        "partitionId": "", "groupId": 0, "netName": net, "ruleName": "viaSize",
        "centerX": x, "centerY": y, "holeDiameter": 12.0078, "viaDiameter": 24.0158,
        "viaType": kind, "topSolderExpansion": None, "bottomSolderExpansion": None,
        "locked": False, "unusedInnerLayers": [],
    }, ticket, rid)


def _pour(net, layer, name, order, path, ticket, rid):
    return _line("POUR", {
        "partitionId": "", "groupId": 0, "netName": net, "layerId": layer, "width": 0.2,
        "name": name, "order": order, "path": path,
        "pourType": {"pourType": "SOLID", "fineness": 8},
        "keepIsland": False, "locked": False, "zIndex": -1,
    }, ticket, rid)


def _poured(target, path, ticket):
    return _line("POURED", {"pourFill": [{"strokeWidth": 0, "fill": True,
                                          "path": path, "id": "fill1"}]},
                 ticket, f'["POURED","{target}"]')


def make(path: Path = HERE / "features.epro2") -> Path:
    lines = [
        _dochead("FOOTPRINT", "fp1"),
        _line("META", {"title": "TEST8"}, 1, "META"),
        _pad("1", -30, 0, {"padType": "RECT", "width": 20, "height": 30, "radius": 2}),
        _pad("2", 30, 0, {"padType": "OVAL", "width": 20, "height": 40}),
        _pad("3", 0, 50, {"padType": "POLYGON", "path": [
            -10, 40, "L", 10, 40, 10, 60, -10, 60, -10, 40]}),
        _pad("4", 0, -40, {"padType": "ELLIPSE", "width": 20, "height": 20},
             layer_id=12, hole={"holeType": "ROUND", "width": 10, "height": 10},
             special=[{"startLayer": 15, "endLayer": 15,
                       "pad": {"padType": "RECT", "width": 30, "height": 30,
                               "radius": 0}}]),
        _pad("5", 50, 50, {"padType": "ELLIPSE", "width": 20, "height": 20},
             layer_id=12, hole={"holeType": "SLOT", "width": 12, "height": 6},
             plated=False),
        _line("ATTR", {"parentId": "", "key": "Footprint", "value": "TEST:FP8"},
              2, "fpname"),

        _dochead("PCB", "pcb1"),
        _line("META", {"title": "PCB1"}, 1, "META"),
        _layer(1, "TOP", "Top Layer", 1),
        _layer(2, "BOTTOM", "Bottom Layer", 2),
        _layer(15, "SIGNAL", "Inner1", 3),
        _layer(16, "SIGNAL", "Inner2", 4),
        _layer(11, "OUTLINE", "Board Outline Layer", 5),
        _layer(3, "TOP_SILK", "Top Silkscreen Layer", 6),
        _layer(4, "BOT_SILK", "Bottom Silkscreen Layer", 7),
        _layer(5, "TOP_SOLDER_MASK", "Top Solder Mask Layer", 8),
        _layer(6, "BOT_SOLDER_MASK", "Bottom Solder Mask Layer", 9),
        _layer(12, "MULTI", "Multi-Layer", 10),

        _phys(3, "", 0, 0, 1, 10),
        _phys(7, "", 0, 0, 2, 11),
        _phys(5, "", 0.394, 3.3, 3, 12),
        _phys(1, "", 1.378, 0, 1000, 13),
        _phys(361, "7628 RC49% 8.6mil", 8.283, 0, 1001, 14),
        _phys(15, "", 0.598, 0, 1002, 15),
        _phys(362, "1.1mm H/HOZ Copper", 41.929, 0, 1003, 16),
        _phys(16, "", 0.598, 0, 1004, 17),
        _phys(363, "7628 RC49% 8.6mil", 8.283, 0, 1005, 18),
        _phys(2, "", 1.378, 0, 9000, 19),
        _phys(6, "", 0.394, 3.3, 10000, 20),
        _phys(8, "", 0, 0, 10001, 21),
        _phys(4, "", 0, 0, 10002, 22),

        _net("", 10),
        _net("GND", 11),
        _net("VIN", 12),
        _net("VOUT", 13),

        _component("U1", 500, 400, 90, 1, "U1", 20),
        _line("COMPONENT", {
            "partitionId": "", "groupId": 0, "layerId": 2,
            "positionX": 800, "positionY": 200, "angle": 30,
            "attrs": {"Name": "C1"}, "locked": False, "zIndex": -1,
        }, 21, "C1"),
        _attr("U1", "Footprint", "fp1", 22),
        _attr("U1", "Designator", "U1", 23),
        _attr("U1", "Device", "LM5155", 24),
        _attr("C1", "Footprint", "fp1", 25),
        _attr("C1", "Designator", "C1", 26),

        _pad_net("U1", "1", "e1", "VIN", 30),
        _pad_net("U1", "2", "e2", "GND", 31),
        _pad_net("U1", "3", "e3", "VOUT", 32),
        _pad_net("U1", "4", "e4", "GND", 33),
        _pad_net("C1", "1", "e1", "VOUT", 34),
        _pad_net("C1", "2", "e2", "GND", 35),
        # A template mapping deleted later; replay must remove it.
        _pad_net("TEMPLATE", "1", "e1", "GND", 36),
        _delete("PAD_NET", '["PAD_NET","TEMPLATE","1","e1"]', 37),

        _line_seg("GND", 1, 450, 400, 550, 400, 10, 40, "line1"),
        _line_seg("VIN", 15, 450, 420, 550, 420, 15, 41, "line2"),
        _line_seg("VOUT", 2, 450, 440, 550, 440, 20, 42, "line3"),
        _line("ARC", {
            "partitionId": "", "groupId": 0, "netName": "GND", "layerId": 16,
            "startX": 600, "startY": 500, "endX": 700, "endY": 500,
            "angle": 90, "width": 10, "locked": False, "zIndex": -1,
        }, 43, "arc1"),
        # Same id written twice with a different type: the later VIA must win.
        _line_seg("GND", 1, 100, 100, 200, 100, 10, 44, "dup"),
        _via("VIN", 250, 250, "NORMAL", 45, "dup"),
        _via("GND", 300, 300, "NORMAL", 46, "via1"),
        _via("VIN", 350, 350, "BLIND", 47, "via2"),

        _pour("VIN", 1, "POUR1", 0,
              [[100, 600, "L", 400, 600, 400, 700, 100, 700, 100, 600]], 50, "pour1"),
        _poured("pour1", [[10.0, 60.0, "L", 40.0, 60.0, 40.0, 70.0, 10.0, 70.0,
                           10.0, 60.0]], 51),
        _pour("VOUT", 2, "POUR2", 1,
              [[100, 200, "L", 300, 200, 300, 300, 100, 300, 100, 200]], 52, "pour2"),
        _line("POLY", {
            "partitionId": "", "groupId": 0, "netName": "", "layerId": 11,
            "width": 10, "path": ["R", 0, 800, 1000, 800, 0, 100],
            "locked": False, "zIndex": -1, "polyType": "BOARD_OUTLINE",
        }, 60, "outline"),
        "this line is not a record\n",

        # A deleted PCB document: pick_pcb() must skip it.
        _dochead("PCB", "dead"),
        _line("DELETE_DOC", {"isDelete": True}, 1),
    ]
    stream = "".join(lines)
    project = {"title": "features", "editorVersion": "4.1.99"}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in (("project2.json", json.dumps(project)),
                           ("features.epru", stream)):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return path


if __name__ == "__main__":
    print(make())
