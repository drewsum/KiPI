"""Write small binary glTF files for the tests, shaped like the ones kicad-cli writes."""

import json
import struct

import numpy as np


def prim(points, indices=None, mode=4, material=None, normals=None, interleave=False):
    return dict(points=np.asarray(points, np.float32), mode=mode, material=material,
                indices=None if indices is None else np.asarray(indices, np.uint16),
                normals=None if normals is None else np.asarray(normals, np.float32),
                interleave=interleave)


def glb(nodes, meshes, materials=(), roots=(0,)):
    """``meshes``: a list of primitive lists (see :func:`prim`); ``materials``: base
    colors (linear RGBA)."""
    blob = bytearray()
    views, accessors = [], []

    def add(arr, kind, ctype, stride=None, data=None):
        while len(blob) % 4:
            blob.append(0)
        view = {"buffer": 0, "byteOffset": len(blob),
                "byteLength": len(data) if data is not None else arr.nbytes}
        if stride:
            view["byteStride"] = stride
        blob.extend(data if data is not None else arr.tobytes())
        views.append(view)
        accessors.append({"bufferView": len(views) - 1, "componentType": ctype,
                          "count": len(arr), "type": kind})
        return len(accessors) - 1

    out_meshes = []
    for prims in meshes:
        out = []
        for p in prims:
            attrs = {}
            if p["interleave"]:             # position and normal in one strided view
                normals = p["normals"] if p["normals"] is not None else np.zeros_like(p["points"])
                both = np.hstack([p["points"], normals]).astype(np.float32)
                attrs["POSITION"] = add(p["points"], "VEC3", 5126, stride=24,
                                        data=both.tobytes())
                if p["normals"] is not None:
                    accessors.append(dict(accessors[-1], byteOffset=12))
                    attrs["NORMAL"] = len(accessors) - 1
            else:
                attrs["POSITION"] = add(p["points"], "VEC3", 5126)
                if p["normals"] is not None:
                    attrs["NORMAL"] = add(p["normals"], "VEC3", 5126)
            entry = {"attributes": attrs, "mode": p["mode"]}
            if p["indices"] is not None:
                entry["indices"] = add(p["indices"], "SCALAR", 5123)
            if p["material"] is not None:
                entry["material"] = p["material"]
            out.append(entry)
        out_meshes.append({"primitives": out})
    doc = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": list(roots)}],
           "nodes": nodes, "meshes": out_meshes, "accessors": accessors,
           "bufferViews": views, "buffers": [{"byteLength": len(blob)}],
           "materials": [{"pbrMetallicRoughness": {"baseColorFactor": list(c)}}
                         for c in materials]}
    js = json.dumps(doc).encode()
    js += b" " * (-len(js) % 4)
    blob += b"\0" * (-len(blob) % 4)
    body = (struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(blob), 0x004E4942) + bytes(blob))
    return b"glTF" + struct.pack("<II", 2, 12 + len(body)) + body


TRI = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]


def kicad_like(refs, named=True):
    """What kicad-cli writes for these parts: a root, one node per part (named by its
    reference) holding a one-triangle mesh, the part's index along X in millimeters."""
    nodes = [{"children": list(range(1, 2 * len(refs) + 1, 2))}]
    for i, ref in enumerate(refs):
        node = {"children": [len(nodes) + 1], "translation": [i / 1000, 0.0016, 0.0]}
        if named:
            node["name"] = ref
        nodes += [node, {"mesh": 0, "name": "=>[0:1:1:3]"}]
    return glb(nodes, [[prim(np.array(TRI) / 1000, material=0)]], [(1, 1, 1, 1)])
