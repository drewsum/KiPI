"""Results files (#99): a zip of JSON and NumPy arrays that opens on any machine and, unlike
the pickles KiPI wrote before 0.8.0, cannot run code when it is opened.

``results.json`` in the zip holds the header and the results. Dataclasses are written as
``{"$o": "module.Class", "f": {field: value}}``, NumPy arrays as ``{"$a": "a/3.npy"}``,
stored next to it in ``.npy`` format (without pickled objects), and the few Python types
JSON lacks (tuples, sets, dicts with non-string keys, complex numbers, paths, NaN and
infinity) are tagged the same way. An object met again is written as ``{"$r": id}``.

Reading rebuilds only KiPI's own dataclasses and enums, so a file cannot name any other
code. Fields a later KiPI added get their defaults, and fields it no longer has are dropped.
"""

from __future__ import annotations

import dataclasses
import importlib
import io
import json
import math
import os
import zipfile
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, Dict, List, Tuple

import numpy as np

from kipi import compat

MANIFEST = "results.json"
_GZIP_MAGIC = b"\x1f\x8b"
# Only classes from these packages are rebuilt.
_PACKAGES = ("kipi.core.", "kipi.dc.", "kipi.ac.", "kipi.noise.")


class LegacyResults(ValueError):
    """A results file saved as a pickle by KiPI 0.7 or earlier."""


def is_legacy(path) -> bool:
    """True for a results file saved before 0.8.0 (a gzipped pickle)."""
    with open(path, "rb") as fh:
        return fh.read(2) == _GZIP_MAGIC


def write(path, header: Dict[str, Any], data: Any) -> Path:
    """Write ``header`` (plain JSON values) and ``data`` to ``path``. The file is written
    beside it first, so a failed save leaves an earlier file as it was."""
    path = Path(path)
    enc = _Encoder()
    doc = dict(header, data=enc.encode(data))
    tmp = path.with_name(path.name + ".part")
    try:
        # Level 1: most of what deflating gains on solver arrays, at a fraction of the time.
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            z.writestr(MANIFEST, json.dumps(doc, allow_nan=False, separators=(",", ":")))
            for name, arr in enc.arrays:
                with z.open(name, "w", force_zip64=True) as fh:
                    np.save(fh, arr, allow_pickle=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def read(path) -> Dict[str, Any]:
    """The header written by :func:`write`, with the results under ``"data"``."""
    try:
        with zipfile.ZipFile(path) as z:
            doc = json.loads(z.read(MANIFEST).decode("utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("Not a KiPI results file")
            doc["data"] = _Decoder(z).decode(doc.get("data"))
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"Not a KiPI results file ({e})") from None
    return doc


# ---------------------------------------------------------------------------

def _class_name(cls: type) -> str:
    if "." in cls.__qualname__ or not cls.__module__.startswith(_PACKAGES):
        raise TypeError(f"Cannot save {cls.__module__}.{cls.__qualname__} in a results file")
    return f"{cls.__module__}.{cls.__qualname__}"


def _resolve(name: str) -> type:
    module, _, attr = str(name).rpartition(".")
    module, attr = compat.moved(module, attr)
    if not module.startswith(_PACKAGES) or any(p.startswith("_") for p in module.split(".")):
        raise ValueError(f"Results file names a class KiPI does not save: {name}")
    try:
        cls = getattr(importlib.import_module(module), attr, None)
    except ImportError:
        cls = None
    if not isinstance(cls, type) or not (dataclasses.is_dataclass(cls) or issubclass(cls, Enum)):
        raise ValueError(f"Results file names a class KiPI does not save: {name}")
    return cls


class _Encoder:
    def __init__(self):
        self.arrays: List[Tuple[str, np.ndarray]] = []
        self._array_names: Dict[int, str] = {}
        self._object_ids: Dict[int, int] = {}

    def encode(self, x: Any) -> Any:
        if x is None or isinstance(x, (bool, str)):
            return x
        if isinstance(x, Enum):
            return {"$e": _class_name(type(x)), "v": self.encode(x.value)}
        if isinstance(x, int):
            return int(x)
        if isinstance(x, float):
            return float(x) if math.isfinite(x) else {"$f": repr(float(x))}
        if isinstance(x, np.ndarray):
            return {"$a": self._array(x)}
        if isinstance(x, np.generic):
            return self.encode(x.item())
        if isinstance(x, complex):
            return {"$c": [self.encode(x.real), self.encode(x.imag)]}
        if dataclasses.is_dataclass(x) and not isinstance(x, type):
            return self._object(x)
        if isinstance(x, PurePath):
            return {"$p": str(x)}
        if isinstance(x, dict):
            if all(isinstance(k, str) and not k.startswith("$") for k in x):
                return {k: self.encode(v) for k, v in x.items()}
            return {"$d": [[self.encode(k), self.encode(v)] for k, v in x.items()]}
        if isinstance(x, list):
            return [self.encode(v) for v in x]
        if isinstance(x, tuple):
            return {"$t": [self.encode(v) for v in x]}
        if isinstance(x, (set, frozenset)):
            return {"$s": [self.encode(v) for v in x]}
        if callable(x):
            return None         # a live function (e.g. a temperature field) is not saved
        raise TypeError(f"Cannot save {type(x).__name__} in a results file")

    def _array(self, a: np.ndarray) -> str:
        name = self._array_names.get(id(a))
        if name is None:
            if a.dtype.hasobject:
                raise TypeError("Cannot save an array of Python objects in a results file")
            name = f"a/{len(self.arrays)}.npy"
            self._array_names[id(a)] = name
            self.arrays.append((name, a))
        return name

    def _object(self, x: Any) -> Dict[str, Any]:
        ref = self._object_ids.get(id(x))
        if ref is not None:
            return {"$r": ref}
        ref = self._object_ids[id(x)] = len(self._object_ids)
        out = {"$o": _class_name(type(x)), "id": ref}
        out["f"] = {f.name: self.encode(getattr(x, f.name)) for f in dataclasses.fields(x)
                    if hasattr(x, f.name)}
        return out


class _Decoder:
    def __init__(self, z: zipfile.ZipFile):
        self._zip = z
        self._arrays: Dict[str, np.ndarray] = {}
        self._objects: Dict[int, Any] = {}

    def decode(self, x: Any) -> Any:
        if isinstance(x, list):
            return [self.decode(v) for v in x]
        if not isinstance(x, dict):
            return x
        if "$o" in x:
            return self._object(x)
        if "$r" in x:
            return self._objects[x["$r"]]
        if "$a" in x:
            return self._array(x["$a"])
        if "$t" in x:
            return tuple(self.decode(v) for v in x["$t"])
        if "$d" in x:
            return {self.decode(k): self.decode(v) for k, v in x["$d"]}
        if "$s" in x:
            return {self.decode(v) for v in x["$s"]}
        if "$c" in x:
            re, im = (self.decode(v) for v in x["$c"])
            return complex(re, im)
        if "$f" in x:
            if x["$f"] not in ("nan", "inf", "-inf"):
                raise ValueError(f"Bad number in results file: {x['$f']!r}")
            return float(x["$f"])
        if "$p" in x:
            return Path(x["$p"])
        if "$e" in x:
            return _resolve(x["$e"])(self.decode(x["v"]))
        return {k: self.decode(v) for k, v in x.items()}

    def _array(self, name: str) -> np.ndarray:
        if name not in self._arrays:
            self._arrays[name] = np.load(io.BytesIO(self._zip.read(name)), allow_pickle=False)
        return self._arrays[name]

    def _object(self, x: Dict[str, Any]) -> Any:
        cls = _resolve(x["$o"])
        if not dataclasses.is_dataclass(cls):
            raise ValueError(f"Results file names a class KiPI does not save: {x['$o']}")
        obj = cls.__new__(cls)
        if "id" in x:
            self._objects[x["id"]] = obj
        fields = {f.name: f for f in dataclasses.fields(cls)}
        state = {k: self.decode(v) for k, v in x.get("f", {}).items() if k in fields}
        setstate = getattr(cls, "__setstate__", None)
        if setstate is not None:        # the class fills in what older files lack
            obj.__setstate__(state)
        else:
            for k, v in state.items():
                object.__setattr__(obj, k, v)
        for name, f in fields.items():
            if not hasattr(obj, name):
                object.__setattr__(obj, name, _default(f))
        return obj


def _default(f: dataclasses.Field) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:
        return f.default_factory()
    return None
