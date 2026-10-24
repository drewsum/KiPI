"""Touchstone network parameter files (``.sNp``), for package PI models (#53).

A vendor's package model is usually an N-port S-parameter file: ports on the package's
balls (or groups of them), where it meets the board, and ports on the die's bumps. Both
Touchstone 1 and 2 (to 2.1) are read: S, Y or Z data, as RI, MA or DB pairs, in any
frequency unit. Touchstone 1 normalizes Y and Z to the reference resistance; Touchstone 2
does not. Mixed-mode data and G or H parameters are refused, and noise data is skipped.

Port names are not part of the format, but most tools write them in comments
(``! Port[1] = VCCINT_BGA``); those are kept where found.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

MAX_FILE_BYTES = 256 << 20          # a many-port package model over a wide sweep
MAX_PORTS = 400
_UNITS = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}
_PORT_NAME = re.compile(r"\bport\s*\[?\s*(\d+)\s*\]?\s*[:=]\s*(\S.*?)\s*$", re.I)
_EXT = re.compile(r"\.s(\d+)p$", re.I)


class TouchstoneError(ValueError):
    """A file that is not Touchstone, or one KiPI cannot use, with why."""


@dataclass
class Network:
    """An N-port's S-parameters at each frequency: ``s`` is (F, N, N), port ``i`` of the
    file at index ``i - 1``, referred to the real ``z0`` of each port."""
    freqs_hz: np.ndarray
    s: np.ndarray
    z0: np.ndarray
    names: List[str] = field(default_factory=list)      # one per port, "Port n" if unnamed

    @property
    def n_ports(self) -> int:
        return self.s.shape[1]

    def s_at(self, freqs) -> np.ndarray:
        """S at ``freqs`` (F', N, N): real and imaginary parts joined by straight lines,
        held at the file's first and last frequency outside them."""
        f = np.asarray(freqs, float)
        if len(self.freqs_hz) == 1:
            return np.repeat(self.s, len(f), axis=0)
        flat = self.s.reshape(len(self.freqs_hz), -1)
        out = np.empty((len(f), flat.shape[1]), complex)
        for j in range(flat.shape[1]):
            out[:, j] = (np.interp(f, self.freqs_hz, flat[:, j].real)
                         + 1j * np.interp(f, self.freqs_hz, flat[:, j].imag))
        return out.reshape(len(f), self.n_ports, self.n_ports)

    def y_at(self, freqs) -> np.ndarray:
        """The admittance matrix (F', N, N, siemens) at ``freqs``: Y exists for the series
        paths a package is made of, where Z need not."""
        s = self.s_at(freqs)
        g = 1.0 / np.sqrt(self.z0)
        eye = np.eye(self.n_ports)
        try:
            # Y = G⁻¹ (I − S)(I + S)⁻¹ G⁻¹, with (I + S) on the right: solve its transpose.
            m = np.linalg.solve(np.swapaxes(eye + s, 1, 2), np.swapaxes(eye - s, 1, 2))
        except np.linalg.LinAlgError:
            raise TouchstoneError("a port of the model is a short circuit (no admittance "
                                  "matrix)") from None
        return np.swapaxes(m, 1, 2) * g[None, :, None] * g[None, None, :]

    def covers(self, f_lo: float, f_hi: float) -> bool:
        return self.freqs_hz[0] <= f_lo * (1 + 1e-9) and self.freqs_hz[-1] >= f_hi * (1 - 1e-9)


def ports_from_name(name: str) -> Optional[int]:
    """N from a ``.sNp`` file name, or None."""
    m = _EXT.search(name or "")
    return int(m.group(1)) if m else None


def read_text(path) -> str:
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise TouchstoneError(f"{path.name} is over {MAX_FILE_BYTES >> 20} MB")
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def read(path) -> Network:
    path = Path(path)
    return parse(read_text(path), ports_from_name(path.name))


def parse(text: str, n_ports: Optional[int] = None) -> Network:
    """The network in Touchstone ``text``. ``n_ports`` comes from a Touchstone 1 file's
    name (``.s4p``: 4); a Touchstone 2 file states it."""
    return _parse(text, n_ports)


@lru_cache(maxsize=16)
def _parse(text: str, n_ports: Optional[int]) -> Network:
    names: Dict[int, str] = {}
    option: Optional[List[str]] = None
    keywords: Dict[str, str] = {}
    numbers: List[str] = []
    version = 1
    section = ""                        # the keyword whose values follow (Touchstone 2)
    in_info = False
    for raw in text.splitlines():
        line, _, comment = raw.partition("!")
        m = _PORT_NAME.search(comment)
        if m:
            names.setdefault(int(m.group(1)), m.group(2))
        line = line.strip()
        if not line:
            continue
        if line.startswith("["):
            key, _, rest = line[1:].partition("]")
            key = key.strip().lower()
            if key == "begin information":
                in_info = True
            elif key == "end information":
                in_info = False
            elif in_info:
                pass
            elif key == "version":
                version = 2
            elif key == "end":
                break
            else:
                keywords[key] = rest.strip()
                section = key
            continue
        if in_info:
            continue
        if line.startswith("#"):
            if option is None:
                option = line[1:].lower().split()
            continue
        if version == 2 and section == "reference":
            keywords["reference"] = (keywords.get("reference", "") + " " + line).strip()
            continue
        if version == 2 and section == "noise data":
            continue
        numbers.extend(line.split())

    if "mixed-mode order" in keywords:
        raise TouchstoneError("mixed-mode (differential) data cannot be used; export the "
                              "model single-ended")
    unit, param, fmt, r = "ghz", "s", "ma", 50.0
    toks = option or []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in _UNITS:
            unit = t
        elif t in ("s", "y", "z", "g", "h"):
            param = t
        elif t in ("ma", "db", "ri"):
            fmt = t
        elif t == "r" and i + 1 < len(toks):
            r = _float(toks[i + 1])
            i += 1
        else:
            raise TouchstoneError(f"cannot read the option line: {' '.join(toks)}")
        i += 1
    if param in ("g", "h"):
        raise TouchstoneError("G and H parameters cannot be used; export S, Y or Z")
    if version == 2:
        if "number of ports" not in keywords:
            raise TouchstoneError("a Touchstone 2 file needs [Number of Ports]")
        n_ports = int(_float(keywords["number of ports"]))
    if not n_ports:
        raise TouchstoneError("the number of ports is unknown: name the file .sNp (.s4p for "
                              "4 ports)")
    n = n_ports
    if not 1 <= n <= MAX_PORTS:
        raise TouchstoneError(f"{n} ports: KiPI reads 1 to {MAX_PORTS}")

    z0 = np.full(n, r)
    if version == 2 and keywords.get("reference"):
        refs = [_float(t) for t in keywords["reference"].split()]
        if len(refs) != n:
            raise TouchstoneError(f"[Reference] gives {len(refs)} impedances for {n} ports")
        z0 = np.array(refs)
    if np.any(z0 <= 0):
        raise TouchstoneError("reference impedances must be positive")
    matrix = keywords.get("matrix format", "full").lower() or "full"
    if matrix not in ("full", "lower", "upper"):
        raise TouchstoneError(f"unknown [Matrix Format] {matrix}")
    order = keywords.get("two-port data order", "").lower()
    if version == 2 and n == 2 and order not in ("12_21", "21_12"):
        raise TouchstoneError("a 2-port Touchstone 2 file needs [Two-Port Data Order]")
    # Touchstone 1 always writes a 2-port as 11 21 12 22.
    column_major = n == 2 and matrix == "full" and (version == 1 or order == "21_12")

    cells = [(i, j) for i in range(n) for j in range(n)
             if matrix == "full" or (matrix == "lower" and j <= i)
             or (matrix == "upper" and j >= i)]
    if column_major:
        cells = [(0, 0), (1, 0), (0, 1), (1, 1)]
    per = 1 + 2 * len(cells)
    values = np.array([_float(t) for t in numbers])
    freqs, data = [], []
    k, noise = 0, False
    while k < len(values):
        f = values[k]
        if freqs and f <= freqs[-1]:
            if n != 2:
                raise TouchstoneError("the frequencies must increase")
            noise = True                # a 2-port's noise data: the frequency drops back
            break
        if k + per > len(values):
            break
        freqs.append(f)
        data.append(values[k + 1:k + per])
        k += per
    if not freqs:
        raise TouchstoneError("no network data")
    expected = keywords.get("number of frequencies")
    if expected and int(_float(expected)) != len(freqs):
        raise TouchstoneError(f"[Number of Frequencies] is {expected}, but the file holds "
                              f"{len(freqs)}")
    if k < len(values) and not noise:
        raise TouchstoneError("the data does not end on a whole frequency")
    pairs = np.array(data).reshape(len(freqs), len(cells), 2)
    a, b = pairs[..., 0], pairs[..., 1]
    if fmt == "ri":
        vals = a + 1j * b
    else:
        mag = a if fmt == "ma" else 10 ** (a / 20)
        vals = mag * np.exp(1j * np.deg2rad(b))
    m = np.zeros((len(freqs), n, n), complex)
    for c, (i, j) in enumerate(cells):
        m[:, i, j] = vals[:, c]
        if matrix != "full":
            m[:, j, i] = vals[:, c]
    f_hz = np.array(freqs) * _UNITS[unit]
    if param != "s":
        if version == 1:                # Touchstone 1 normalizes Y and Z to R
            m = m * r if param == "z" else m / r
        m = _to_s(m, param, z0)
    if not np.all(np.isfinite(m)):
        raise TouchstoneError("the data has values that are not finite")
    labels = [names.get(p + 1, f"Port {p + 1}") for p in range(n)]
    return Network(f_hz, m, z0, labels)


def _to_s(m: np.ndarray, param: str, z0: np.ndarray) -> np.ndarray:
    g = np.sqrt(z0)
    eye = np.eye(m.shape[1])
    if param == "z":
        x = m / g[None, :, None] / g[None, None, :]         # normalized Z
        num, den = x - eye, x + eye
    else:
        x = m * g[None, :, None] * g[None, None, :]         # normalized Y
        num, den = eye - x, eye + x
    try:
        # S = num · den⁻¹
        return np.swapaxes(np.linalg.solve(np.swapaxes(den, 1, 2), np.swapaxes(num, 1, 2)),
                           1, 2)
    except np.linalg.LinAlgError:
        raise TouchstoneError("cannot convert the data to S-parameters") from None


def _float(t: str) -> float:
    try:
        return float(t)
    except ValueError:
        raise TouchstoneError(f"cannot read the number {t!r}") from None


def write(path, freqs_hz, s: np.ndarray, z0: float = 50.0,
          names: Tuple[str, ...] = ()) -> Path:
    """Write S-parameters as Touchstone 1 RI (for tests and examples)."""
    path = Path(path)
    n = s.shape[1]
    lines = [f"! Port[{i + 1}] = {name}" for i, name in enumerate(names)]
    lines.append(f"# Hz S RI R {z0:g}")
    cells = ([(0, 0), (1, 0), (0, 1), (1, 1)] if n == 2
             else [(i, j) for i in range(n) for j in range(n)])
    for f, m in zip(freqs_hz, s):
        vals = " ".join(f"{m[i, j].real:.12g} {m[i, j].imag:.12g}" for i, j in cells)
        lines.append(f"{f:.12g} {vals}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def s_from_y(y: np.ndarray, z0: float = 50.0) -> np.ndarray:
    """S from an admittance matrix (F, N, N), all ports at ``z0``."""
    return _to_s(np.asarray(y, complex), "y", np.full(y.shape[1], z0))
