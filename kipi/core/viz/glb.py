"""Read the triangles of a binary glTF (.glb) file, grouped by named node.

KiPI reads the files KiCad's ``kicad-cli pcb export glb`` writes (see
:mod:`kipi.core.kicad_io.models3d`): one node per part, named by its reference, holding
the part's meshes. VTK has a glTF reader, but it names its blocks ``Node_<n>`` and drops
those names, so this small reader keeps them. It covers what a static model needs: node
transforms, triangle meshes (lists, strips and fans) and each material's base color.
Textures, animation and compressed meshes are not read.

Coordinates stay as glTF has them: meters, Y up.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

_MAGIC = b"glTF"
_JSON, _BIN = 0x4E4F534A, 0x004E4942
_DTYPES = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16,
           5125: np.uint32, 5126: np.float32}
_WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_TRIANGLES, _STRIP, _FAN = 4, 5, 6
_DEFAULT_COLOR = (0.8, 0.8, 0.8)


@dataclass
class Part:
    """Triangles in glTF coordinates; ``rgb`` (uint8) per triangle; ``normals`` per point,
    or None when a mesh of the part has none."""
    points: np.ndarray
    tris: np.ndarray
    rgb: np.ndarray
    normals: Optional[np.ndarray] = None


def _chunks(data: bytes) -> Tuple[dict, bytes]:
    if len(data) < 20 or data[:4] != _MAGIC:
        raise ValueError("not a binary glTF file")
    version, length = struct.unpack_from("<II", data, 4)
    if version != 2:
        raise ValueError(f"glTF version {version} is not supported")
    doc, binary, pos = None, b"", 12
    while pos + 8 <= min(length, len(data)):
        size, kind = struct.unpack_from("<II", data, pos)
        body = data[pos + 8:pos + 8 + size]
        if kind == _JSON:
            doc = json.loads(body.decode("utf-8"))
        elif kind == _BIN and not binary:
            binary = body
        pos += 8 + size
    if doc is None:
        raise ValueError("glTF file has no JSON chunk")
    return doc, binary


def _accessor(doc: dict, binary: bytes, index: int) -> np.ndarray:
    acc = doc["accessors"][index]
    width = _WIDTH[acc["type"]]
    dtype = np.dtype(_DTYPES[acc["componentType"]])
    count = int(acc["count"])
    if "bufferView" not in acc:             # all zeros (a sparse accessor's base)
        return np.zeros((count, width), dtype)
    view = doc["bufferViews"][acc["bufferView"]]
    if view.get("buffer", 0) != 0 or "uri" in doc["buffers"][view.get("buffer", 0)]:
        raise ValueError("glTF data outside the .glb file is not supported")
    offset = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    item = dtype.itemsize * width
    stride = view.get("byteStride") or item
    if count == 0:
        return np.zeros((0, width), dtype)
    if stride == item:
        arr = np.frombuffer(binary, dtype, count=count * width, offset=offset)
    else:                                   # interleaved: copy each item out
        raw = np.frombuffer(binary, np.uint8, count=stride * (count - 1) + item, offset=offset)
        rows = np.lib.stride_tricks.as_strided(raw, shape=(count, item), strides=(stride, 1))
        arr = np.ascontiguousarray(rows).view(dtype)
    return arr.reshape(count, width)


def _quat_matrix(q) -> np.ndarray:
    x, y, z, w = (float(v) for v in q)
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def node_matrix(node: dict) -> np.ndarray:
    """A node's local transform as a 4x4 matrix (glTF stores ``matrix`` column-major)."""
    if "matrix" in node:
        return np.asarray(node["matrix"], dtype=float).reshape(4, 4).T
    m = np.eye(4)
    m[:3, :3] = _quat_matrix(node.get("rotation", (0, 0, 0, 1))) * np.asarray(
        node.get("scale", (1, 1, 1)), dtype=float)
    m[:3, 3] = node.get("translation", (0, 0, 0))
    return m


def _triangles(mode: int, idx: np.ndarray) -> np.ndarray:
    if mode == _TRIANGLES:
        return idx[:len(idx) // 3 * 3].reshape(-1, 3)
    n = len(idx) - 2
    if n <= 0:
        return np.zeros((0, 3), np.int64)
    k = np.arange(n)
    if mode == _STRIP:                      # every other triangle is wound the other way
        odd = k % 2 == 1
        a, b = np.where(odd, idx[k + 1], idx[k]), np.where(odd, idx[k], idx[k + 1])
        return np.column_stack([a, b, idx[k + 2]])
    return np.column_stack([np.full(n, idx[0]), idx[k + 1], idx[k + 2]])      # fan


def _srgb8(linear) -> np.ndarray:
    """glTF base colors are linear; the view shows sRGB."""
    c = np.clip(np.asarray(linear[:3], dtype=float), 0.0, 1.0)
    s = np.where(c <= 0.0031308, 12.92 * c, 1.055 * np.power(c, 1 / 2.4) - 0.055)
    return np.round(s * 255).astype(np.uint8)


def _material_rgb(doc: dict, index: Optional[int]) -> np.ndarray:
    if index is None:
        return np.round(np.asarray(_DEFAULT_COLOR) * 255).astype(np.uint8)
    pbr = doc.get("materials", [])[index].get("pbrMetallicRoughness", {})
    return _srgb8(pbr.get("baseColorFactor", (*_DEFAULT_COLOR, 1.0)))


def _mesh_pieces(doc: dict, binary: bytes, mesh_index: int, matrix: np.ndarray):
    """``(points, tris, rgb, normals)`` for each triangle primitive of a mesh."""
    rot = matrix[:3, :3]
    try:
        normal_rot = np.linalg.inv(rot).T
    except np.linalg.LinAlgError:
        normal_rot = rot
    for prim in doc["meshes"][mesh_index].get("primitives", []):
        mode = prim.get("mode", _TRIANGLES)
        attrs = prim.get("attributes", {})
        if mode not in (_TRIANGLES, _STRIP, _FAN) or "POSITION" not in attrs:
            continue
        pts = _accessor(doc, binary, attrs["POSITION"]).astype(float)
        if "indices" in prim:
            idx = _accessor(doc, binary, prim["indices"]).ravel().astype(np.int64)
        else:
            idx = np.arange(len(pts), dtype=np.int64)
        tris = _triangles(mode, idx)
        tris = tris[(tris < len(pts)).all(axis=1)]
        if not len(tris):
            continue
        pts = pts @ rot.T + matrix[:3, 3]
        normals = None
        if "NORMAL" in attrs:
            n = _accessor(doc, binary, attrs["NORMAL"]).astype(float) @ normal_rot.T
            length = np.linalg.norm(n, axis=1, keepdims=True)
            normals = n / np.where(length > 0, length, 1.0)
        rgb = np.tile(_material_rgb(doc, prim.get("material")), (len(tris), 1))
        yield pts, tris, rgb, normals


def _merge(pieces: List[tuple]) -> Part:
    offsets = np.cumsum([0] + [len(p[0]) for p in pieces[:-1]])
    normals = None
    if all(p[3] is not None for p in pieces):
        normals = np.concatenate([p[3] for p in pieces])
    return Part(points=np.concatenate([p[0] for p in pieces]),
                tris=np.concatenate([p[1] + o for p, o in zip(pieces, offsets)]),
                rgb=np.concatenate([p[2] for p in pieces]), normals=normals)


def read_glb(data: bytes, names: Optional[Iterable[str]] = None) -> Dict[str, Part]:
    """The triangles under each named node, keyed by the node's name.

    Triangles go to the outermost node whose name is one of ``names`` or, without
    ``names``, to the outermost node with any name. Triangles under no such node are
    keyed ``""``.
    """
    doc, binary = _chunks(data)
    wanted = set(names) if names is not None else None
    nodes = doc.get("nodes", [])
    scenes = doc.get("scenes", [])
    roots = (scenes[doc.get("scene", 0)].get("nodes", []) if scenes
             else [i for i in range(len(nodes))
                   if not any(i in n.get("children", []) for n in nodes)])
    pieces: Dict[str, List[tuple]] = {}
    stack = [(i, np.eye(4), None, 0) for i in roots]
    while stack:
        i, parent, key, depth = stack.pop()
        if depth > 64:                       # a cycle; glTF forbids them
            continue
        node = nodes[i]
        matrix = parent @ node_matrix(node)
        name = node.get("name") or ""
        if key is None and name and (wanted is None or name in wanted):
            key = name
        if "mesh" in node:
            for piece in _mesh_pieces(doc, binary, node["mesh"], matrix):
                pieces.setdefault(key or "", []).append(piece)
        stack.extend((c, matrix, key, depth + 1) for c in node.get("children", []))
    return {k: _merge(v) for k, v in pieces.items()}
