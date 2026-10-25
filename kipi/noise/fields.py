"""The fields of a switcher's sources (#146): the magnetic field of its current loops and
the electric field of its switch nodes, near the board, and what they say about emissions.

**H field.** Every current loop the solve has (each capacitor's loop through the copper
and the inductor's equivalent loop, :mod:`kipi.noise.extract.inductive`) is a set of
straight elements, each carrying its share of the loop's current. The field of a straight
element at a point is Biot–Savart's closed form, H = I/(4πρ)·(sin θ₂ − sin θ₁) round it,
and the loop's field per ampere is the sum. A plane under the loop answers with eddy
currents: above a few megahertz it mirrors the loop (image elements carrying the opposite
horizontal current), while at low frequency the loop's real return current, spread through
the plane, is what the conduction solve traced. So a loop's field at a frequency is
A + Γ·B + (1 − Γ)·C + T·D: its elements' direct field, their images' by the plane's
mirroring Γ, the plane's own current by what the plane does not mirror, and the elements
on the far side of the plane by what it lets through (:func:`peec.plane_response`). Each
map is computed once per ampere; the harmonics and the time animation only weight them.

**E field.** A switch node's copper is a charged conductor over the planes next to it:
each patch of it carries ε0·εr·A/h per volt (its capacitance to the nearest plane), and
the field above the board is that of the patches' charges and their images in the plane,
E = q/(4πε0 r²) each. The whole node swings together, so one map per volt serves every
harmonic and the animation.

**Hotspots and emissions.** On a plane a few millimetres above the board (where a
near-field probe would be) the fields are evaluated at every harmonic that matters; where
they are largest, and at which harmonic, are the hotspots. For radiation, each loop (with
its images: a loop over a plane radiates as the loop it makes with its image, far smaller)
is a magnetic dipole of moment m = ½·Σ I·r × dl; a small loop's far field at distance r
is E = 1.32·10⁻¹⁴·f²·|m|/r V/m (Ott), summed over the loops as phasors. That is the
differential-mode estimate of a bare board: common-mode currents on cables, enclosures
and the test site are not modelled, so it is an indicator, not a compliance prediction.
The CISPR 32 class B limit at 3 m is shown beside it for scale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from kipi.noise.extract import peec
from kipi.noise.waveforms import Spectrum, ifft_periodic

EPS0_F_PER_M = 8.8541878128e-12
_CHUNK = 1_500_000                      # elements × points per block
MAP_NAMES = ("direct", "image", "plane", "through")
NEAR_FIELD_MM = 5.0                     # the hotspot plane's height over the top copper
FAR_FIELD_M = 3.0                       # the radiated estimate's distance
HOTSPOTS = 5
HOTSPOT_APART_MM = 3.0
HOTSPOT_POINTS = 4000                   # the hotspot grid's size, at most
MAP_POINTS = 8000                       # the report's field map's grid, at most
MAP_CELL_MM = 0.5                       # and its finest cell
HARMONIC_FLOOR = 1e-3                   # harmonics under this share of the largest are skipped
# CISPR 32 class B radiated limits at 3 m, quasi-peak, dBµV/m, by band.
CISPR_B_3M = ((30e6, 230e6, 40.0), (230e6, 1e9, 47.0))


@dataclass
class HSource:
    """A current loop as a field source: its elements (a→b, mm) with the amperes each
    carries per ampere of the loop, the plane under it (its copper's z_bottom, z_top; None
    without one) and the loop's current."""
    key: str
    label: str
    phase: int
    a: np.ndarray                       # (N, 3)
    b: np.ndarray
    weights: np.ndarray                 # (N,)
    radius: np.ndarray                  # (N,) the elements' GMD radii, mm
    plane: Optional[Tuple[float, float]]
    thickness_mm: float
    size_mm: float
    current: Spectrum

    def sides(self) -> np.ndarray:
        if self.plane is None:
            return np.ones(len(self.a), dtype=int)
        z = (self.a[:, 2] + self.b[:, 2]) / 2
        return np.where(z > self.plane[1] + 1e-6, 1, np.where(z < self.plane[0] - 1e-6, -1, 0))

    def side_of(self, z_mm: float) -> int:
        """Which side of the plane a height is on (above without a plane)."""
        if self.plane is None:
            return 1
        return 1 if z_mm >= (self.plane[0] + self.plane[1]) / 2 else -1

    def weights_at(self, freqs: np.ndarray) -> Dict[str, np.ndarray]:
        """Each map's complex weight at the frequencies ``freqs``."""
        freqs = np.asarray(freqs, float)
        one = np.ones(len(freqs), complex)
        if self.plane is None:
            return {"direct": one, "image": 0 * one, "plane": one, "through": one}
        gamma, trans = peec.plane_response(freqs, self.thickness_mm, self.size_mm)
        return {"direct": one, "image": gamma, "plane": 1 - gamma, "through": trans}

    def maps(self, pts: np.ndarray, side: int) -> Dict[str, np.ndarray]:
        """The field per ampere at ``pts`` (P, 3) mm, all on ``side`` of the plane: the
        maps there are, each (P, 3) in A/m."""
        sides = self.sides()
        out = {}
        if self.plane is None:
            out["direct"] = biot_savart(self.a, self.b, self.weights, self.radius, pts)
            return out
        own = sides == side
        if own.any():
            out["direct"] = biot_savart(self.a[own], self.b[own], self.weights[own],
                                        self.radius[own], pts)
            z_face = self.plane[1] if side > 0 else self.plane[0]
            a_img, b_img = self.a[own].copy(), self.b[own].copy()
            a_img[:, 2] = 2 * z_face - a_img[:, 2]
            b_img[:, 2] = 2 * z_face - b_img[:, 2]
            out["image"] = -biot_savart(a_img, b_img, self.weights[own], self.radius[own], pts)
        inside = sides == 0
        if inside.any():
            out["plane"] = biot_savart(self.a[inside], self.b[inside], self.weights[inside],
                                       self.radius[inside], pts)
        far = sides == -side
        if far.any():
            out["through"] = biot_savart(self.a[far], self.b[far], self.weights[far],
                                         self.radius[far], pts)
        return out

    def moments(self) -> Dict[str, np.ndarray]:
        """The loop's magnetic dipole moment per ampere, A·m², by map: its elements'
        ½·Σ w·r × dl and their images' (on the side the plane faces most of the loop)."""
        sides = self.sides()
        mid = (self.a + self.b) / 2 * 1e-3
        dl = (self.b - self.a) * 1e-3
        m = 0.5 * np.cross(mid, dl) * self.weights[:, None]
        if self.plane is None:
            return {"direct": m.sum(axis=0)}
        side = 1 if (sides > 0).sum() >= (sides < 0).sum() else -1
        own = sides == side
        out = {"direct": m[own].sum(axis=0) if own.any() else np.zeros(3)}
        if own.any():
            z_face = (self.plane[1] if side > 0 else self.plane[0]) * 1e-3
            img_mid, img_dl = mid[own].copy(), dl[own].copy()
            img_mid[:, 2] = 2 * z_face - img_mid[:, 2]
            img_dl[:, 2] = -img_dl[:, 2]
            out["image"] = -(0.5 * np.cross(img_mid, img_dl)
                             * self.weights[own][:, None]).sum(axis=0)
        inside = sides == 0
        if inside.any():
            out["plane"] = m[inside].sum(axis=0)
        far = sides == -side
        if far.any():
            out["through"] = m[far].sum(axis=0)
        return out

    def xy_extent(self) -> Tuple[float, float, float, float]:
        pts = np.concatenate([self.a, self.b])
        return (float(pts[:, 0].min()), float(pts[:, 1].min()),
                float(pts[:, 0].max()), float(pts[:, 1].max()))


@dataclass
class ESource:
    """A switch node's copper as charged patches: each at ``xyz`` (mm) with ``q_per_v``
    farads (its charge per volt), its image in the plane at ``image_z`` (mm; NaN without
    one), and the node's voltage."""
    key: str
    label: str
    phase: int
    xyz: np.ndarray                     # (P, 3)
    q_per_v: np.ndarray                 # (P,)
    image_z: np.ndarray                 # (P,)
    cell_mm: float
    voltage: Spectrum

    def map(self, pts: np.ndarray) -> np.ndarray:
        """The field per volt at ``pts`` (P, 3) mm, (P, 3) in V/m."""
        out = patch_field(self.xyz, self.q_per_v, self.cell_mm, pts)
        has = np.isfinite(self.image_z)
        if has.any():
            img = self.xyz[has].copy()
            img[:, 2] = 2 * self.image_z[has] - img[:, 2]
            out -= patch_field(img, self.q_per_v[has], self.cell_mm, pts)
        return out

    def xy_extent(self) -> Tuple[float, float, float, float]:
        return (float(self.xyz[:, 0].min()), float(self.xyz[:, 1].min()),
                float(self.xyz[:, 0].max()), float(self.xyz[:, 1].max()))


@dataclass
class FieldSources:
    """Every field source of a solve, with the board's copper heights."""
    h: List[HSource] = field(default_factory=list)
    e: List[ESource] = field(default_factory=list)
    fsw_hz: float = 0.0
    z_top_mm: float = 0.0               # the top copper's upper face
    z_bottom_mm: float = 0.0
    # The board outline's bounds (x0, y0, x1, y1) mm, for the report's map; None without one.
    board_bounds: Optional[Tuple[float, float, float, float]] = None

    def get(self, key: str):
        return next((s for s in [*self.h, *self.e] if s.key == key), None)

    def of_kind(self, kind: str) -> list:
        return list(self.h if kind == "H" else self.e)


def biot_savart(a: np.ndarray, b: np.ndarray, w: np.ndarray, r0: np.ndarray,
                pts: np.ndarray) -> np.ndarray:
    """H (A/m) at ``pts`` (P, 3) mm of straight elements a→b (N, 3) mm carrying ``w``
    amperes, each regularised within its radius ``r0``: Σ w/(4πρ)·(sin θ₂ − sin θ₁)·φ̂."""
    out = np.zeros((len(pts), 3))
    if not len(a) or not len(pts):
        return out
    d = b - a
    length = np.linalg.norm(d, axis=1)
    ok = length > 1e-9
    a, d, w, r0, length = a[ok], d[ok], w[ok], r0[ok], length[ok]
    u = d / length[:, None]
    step = max(1, _CHUNK // len(pts))
    for c0 in range(0, len(a), step):
        c = slice(c0, c0 + step)
        rel = pts[None, :, :] - a[c, None, :]                          # (C, P, 3)
        s = np.einsum("cpk,ck->cp", rel, u[c])
        perp = rel - s[:, :, None] * u[c, None, :]
        rho2 = np.einsum("cpk,cpk->cp", perp, perp) + (r0[c] ** 2)[:, None]
        ln = length[c][:, None]
        geom = (ln - s) / np.sqrt((ln - s) ** 2 + rho2) + s / np.sqrt(s * s + rho2)
        phi = np.cross(u[c, None, :], perp)                            # (C, P, 3)
        out += np.einsum("c,cpk->pk", w[c], phi * (geom / rho2)[:, :, None])
    return out * (1e3 / (4 * math.pi))                                 # 1/mm → 1/m


def patch_field(xyz: np.ndarray, q: np.ndarray, cell_mm: float, pts: np.ndarray) -> np.ndarray:
    """E (V/m) at ``pts`` (P, 3) mm of point charges ``q`` (C) at ``xyz`` (N, 3) mm,
    each spread over a patch ``cell_mm`` across."""
    out = np.zeros((len(pts), 3))
    if not len(xyz) or not len(pts):
        return out
    step = max(1, _CHUNK // len(pts))
    r_min2 = (cell_mm / 2 * 1e-3) ** 2
    for c0 in range(0, len(xyz), step):
        c = slice(c0, c0 + step)
        rel = (pts[None, :, :] - xyz[c, None, :]) * 1e-3                # m
        r2 = np.einsum("cpk,cpk->cp", rel, rel) + r_min2
        out += np.einsum("c,cpk->pk", q[c], rel / (r2 ** 1.5)[:, :, None])
    return out / (4 * math.pi * EPS0_F_PER_M)


def slice_grid(bounds: Tuple[float, float, float, float], z_mm: float, cell_mm: float,
               pad_mm: float = 0.0) -> Tuple[np.ndarray, int, int]:
    """Points (P, 3) of a horizontal grid over ``bounds`` (x0, y0, x1, y1) grown by
    ``pad_mm``, at ``z_mm``, ``cell_mm`` apart, row by row (y outer): and (nx, ny)."""
    x0, y0, x1, y1 = bounds
    x0, y0, x1, y1 = x0 - pad_mm, y0 - pad_mm, x1 + pad_mm, y1 + pad_mm
    nx = max(2, int(round((x1 - x0) / cell_mm)) + 1)
    ny = max(2, int(round((y1 - y0) / cell_mm)) + 1)
    xs = np.linspace(x0, x1, nx)
    ys = np.linspace(y0, y1, ny)
    gx, gy = np.meshgrid(xs, ys)
    pts = np.column_stack([gx.ravel(), gy.ravel(), np.full(gx.size, z_mm)])
    return pts, nx, ny


def extent(sources) -> Tuple[float, float, float, float]:
    boxes = [s.xy_extent() for s in sources]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _map_wave(src: HSource, name: str, n_t: int, fsw: float) -> np.ndarray:
    """A map's current over the period: the loop's alternating current weighted by the
    map's response at each harmonic (the DC part of an inductor's current is left out:
    a steady field couples nothing)."""
    sp = src.current
    freqs = fsw * np.arange(1, len(sp.c) + 1)
    return ifft_periodic(0.0, sp.c * src.weights_at(freqs)[name], n_t)


class FieldSlice:
    """The chosen sources' alternating field on a horizontal slice, as the 3D view and
    the animation need it: maps per ampere or volt, stacked, and each map's waveform over
    a period."""

    def __init__(self, sources: FieldSources, kind: str, chosen: Sequence, z_mm: float,
                 n_t: int, cell_mm: Optional[float] = None, pad_mm: Optional[float] = None):
        self.kind = kind                                   # "H" or "E"
        self.unit = "A/m" if kind == "H" else "V/m"
        self.sources = list(chosen)
        bounds = extent(chosen)
        span = max(bounds[2] - bounds[0], bounds[3] - bounds[1], 1.0)
        pad = max(3.0, 0.5 * span) if pad_mm is None else pad_mm
        cell = float(np.clip((span + 2 * pad) / 120, 0.15, 1.0)) if cell_mm is None else cell_mm
        self.pts, self.nx, self.ny = slice_grid(bounds, z_mm, cell, pad)
        self.cell_mm = cell
        self.z_mm = z_mm
        maps, waves = [], []
        for s in chosen:
            if kind == "H":
                for name, m in s.maps(self.pts, s.side_of(z_mm)).items():
                    maps.append(m)
                    waves.append(_map_wave(s, name, n_t, sources.fsw_hz))
            else:
                maps.append(s.map(self.pts))
                waves.append(ifft_periodic(0.0, s.voltage.c, n_t))
        self.maps = np.stack(maps) if maps else np.zeros((0, len(self.pts), 3))
        self.waves = np.stack(waves) if waves else np.zeros((0, n_t))     # (M, T)

    def at(self, t: Sequence[int]) -> np.ndarray:
        """The field at the period's samples ``t``: (len(t), P, 3)."""
        return np.einsum("mt,mpk->tpk", self.waves[:, list(t)], self.maps)

    def peak(self, stride: int = 1) -> Tuple[np.ndarray, np.ndarray]:
        """Each point's largest |field| over the period (every ``stride`` samples) and
        the field there then: (P,), (P, 3)."""
        n_t = self.waves.shape[1]
        best = np.zeros(len(self.pts))
        vec = np.zeros((len(self.pts), 3))
        block = max(1, 4_000_000 // max(len(self.pts), 1))
        times = list(range(0, n_t, stride))
        for i in range(0, len(times), block):
            f = self.at(times[i:i + block])
            mag = np.linalg.norm(f, axis=2)                 # (T, P)
            k = np.argmax(mag, axis=0)
            m = mag[k, np.arange(len(self.pts))]
            better = m > best
            best[better] = m[better]
            vec[better] = f[k[better], np.flatnonzero(better)]
        return best, vec

    def frames(self, count: int) -> np.ndarray:
        """``count`` frames of the field over one period, (count, P, 3), single precision."""
        n_t = self.waves.shape[1]
        t = np.linspace(0, n_t, count, endpoint=False).astype(int)
        return self.at(t).astype(np.float32)


# -------------------------------------------------------------------- hotspots, EMI
@dataclass
class Hotspot:
    x_mm: float
    y_mm: float
    value: float                        # A/m or V/m, the harmonic's peak amplitude
    f_hz: float                         # the harmonic it is largest at
    source: str                         # the source contributing most there
    unit: str


@dataclass
class FieldMap:
    """Each point's strongest harmonic (peak amplitude) on the hotspot plane, for the
    report's board map: ``values`` (ny, nx) over ``bounds`` (x0, y0, x1, y1) mm, row by
    row from y0."""
    kind: str                           # "H" or "E"
    unit: str
    values: np.ndarray
    bounds: Tuple[float, float, float, float]


@dataclass
class EmiReport:
    """Where the near fields are strongest and a first estimate of what the loops radiate
    (:mod:`kipi.noise.fields`)."""
    height_mm: float
    distance_m: float
    h_hotspots: List[Hotspot] = field(default_factory=list)
    e_hotspots: List[Hotspot] = field(default_factory=list)
    h_map: Optional[FieldMap] = None
    e_map: Optional[FieldMap] = None
    radiated_f: Optional[np.ndarray] = None       # (F,) Hz
    radiated_dbuv: Optional[np.ndarray] = None    # (F,) dBµV/m at distance_m
    limit_dbuv: Optional[np.ndarray] = None       # (F,) the CISPR 32 B limit, NaN off band
    worst_f_hz: float = 0.0
    worst_dbuv: float = -math.inf
    worst_limit_dbuv: float = math.nan
    worst_source: str = ""
    notes: List[str] = field(default_factory=list)

    def lines(self) -> List[str]:
        out = []
        for spots, what in ((self.h_hotspots, "H"), (self.e_hotspots, "E")):
            if spots:
                s = spots[0]
                out.append(f"Near field {self.height_mm:g} mm over the board: strongest {what} "
                           f"{s.value:.3g} {s.unit} at ({s.x_mm:.1f}, {s.y_mm:.1f}) mm, at "
                           f"{s.f_hz / 1e6:.4g} MHz, from {s.source}")
        if self.radiated_dbuv is not None and np.isfinite(self.worst_dbuv):
            lim = (f" (CISPR 32 class B limit {self.worst_limit_dbuv:.0f})"
                   if np.isfinite(self.worst_limit_dbuv) else " (below the 30 MHz band)")
            out.append(f"Radiated estimate at {self.distance_m:g} m: {self.worst_dbuv:.1f} "
                       f"dBµV/m at {self.worst_f_hz / 1e6:.4g} MHz{lim}, mostly "
                       f"{self.worst_source}; the loops only, no cables or enclosure")
        return out


def cispr_limit(f: np.ndarray) -> np.ndarray:
    out = np.full(len(f), np.nan)
    for lo, hi, lim in CISPR_B_3M:
        out[(f >= lo) & (f < hi)] = lim
    return out


def _kept_harmonics(spectra: Sequence[Spectrum]) -> np.ndarray:
    """The harmonics (indices) any spectrum has at HARMONIC_FLOOR of its largest."""
    keep = np.zeros(max((len(sp.c) for sp in spectra), default=0), dtype=bool)
    for sp in spectra:
        amp = np.abs(sp.c)
        if amp.size and amp.max() > 0:
            keep[:len(amp)] |= amp >= HARMONIC_FLOOR * amp.max()
    return np.flatnonzero(keep)


def _hotspots(srcs, kind: str, freqs: np.ndarray, pts: np.ndarray, unit: str,
              block: int = 16) -> Tuple[List[Hotspot], np.ndarray]:
    """Where |field| of any harmonic is largest on ``pts``, the harmonic, and the source
    contributing most there; and every point's largest |field| (P,)."""
    if kind == "H":
        idx = _kept_harmonics([s.current for s in srcs])
    else:
        idx = _kept_harmonics([s.voltage for s in srcs])
    if not len(idx):
        return [], np.zeros(len(pts))
    prepared = []
    for s in srcs:
        if kind == "H":
            prepared.append((s.maps(pts, s.side_of(pts[0, 2])), s.weights_at(freqs[idx]),
                             s.current.c[idx]))
        else:
            prepared.append(({"direct": s.map(pts)}, {"direct": np.ones(len(idx), complex)},
                             s.voltage.c[idx]))
    best = np.zeros(len(pts))
    k_best = np.zeros(len(pts), dtype=int)
    per_best = np.zeros((len(srcs), len(pts)))
    for f0 in range(0, len(idx), block):
        fs = slice(f0, f0 + block)
        n = len(idx[fs])
        total = np.zeros((n, len(pts), 3), complex)
        per = []
        for maps, wts, c in prepared:
            unit_f = np.zeros((n, len(pts), 3), complex)
            for name, m in maps.items():
                unit_f += wts[name][fs, None, None] * m[None, :, :]
            contrib = 2 * c[fs, None, None] * unit_f
            total += contrib
            per.append(np.linalg.norm(contrib, axis=2))
        mag = np.linalg.norm(total, axis=2)                     # (n, P)
        k = mag.argmax(axis=0)
        m = mag[k, np.arange(len(pts))]
        better = m > best
        best[better] = m[better]
        k_best[better] = idx[fs][k[better]]
        for j, p in enumerate(per):
            per_best[j, better] = p[k[better], np.flatnonzero(better)]
    order = np.argsort(-best)
    spots: List[Hotspot] = []
    for i in order:
        if best[i] <= 0 or len(spots) >= HOTSPOTS:
            break
        if any(math.hypot(pts[i, 0] - s.x_mm, pts[i, 1] - s.y_mm) < HOTSPOT_APART_MM
               for s in spots):
            continue
        spots.append(Hotspot(float(pts[i, 0]), float(pts[i, 1]), float(best[i]),
                             float(freqs[k_best[i]]), srcs[int(np.argmax(per_best[:, i]))].label,
                             unit))
    return spots, best


def _field_map(srcs, kind: str, freqs: np.ndarray, z_mm: float, unit: str, box,
               board) -> FieldMap:
    """Every point's strongest harmonic over the board (and the sources, with room round
    them), on a grid of its own: the hotspots' grid hugs the sources, and the field is
    still there at its edge."""
    x0, y0, x1, y1 = box
    x0, y0, x1, y1 = x0 - 3.0, y0 - 3.0, x1 + 3.0, y1 + 3.0
    if board is not None:
        x0, y0 = min(x0, board[0]), min(y0, board[1])
        x1, y1 = max(x1, board[2]), max(y1, board[3])
    cell = max(MAP_CELL_MM, math.sqrt(max(x1 - x0, 1.0) * max(y1 - y0, 1.0) / MAP_POINTS))
    pts, nx, ny = slice_grid((x0, y0, x1, y1), z_mm, cell)
    _, best = _hotspots(srcs, kind, freqs, pts, unit)
    return FieldMap(kind, unit, best.reshape(ny, nx),
                    (float(pts[0, 0]), float(pts[0, 1]), float(pts[-1, 0]), float(pts[-1, 1])))


def near_field_report(sources: FieldSources, freqs: np.ndarray,
                      height_mm: float = NEAR_FIELD_MM, distance_m: float = FAR_FIELD_M
                      ) -> EmiReport:
    """Hotspots of H and E ``height_mm`` over the top copper and the loops' radiated
    estimate at ``distance_m`` (:class:`EmiReport`)."""
    rep = EmiReport(height_mm, distance_m)
    z = sources.z_top_mm + height_mm
    freqs = np.asarray(freqs, float)
    for kind, srcs, unit in (("H", sources.h, "A/m"), ("E", sources.e, "V/m")):
        if not srcs or not len(freqs):
            continue
        box = extent(srcs)
        span = max(box[2] - box[0], box[3] - box[1], 1.0) + 6.0
        cell = max(1.0, span / math.sqrt(HOTSPOT_POINTS))
        pts, _, _ = slice_grid(box, z, cell, pad_mm=3.0)
        spots, _ = _hotspots(srcs, kind, freqs, pts, unit)
        fmap = _field_map(srcs, kind, freqs, z, unit, box,
                          getattr(sources, "board_bounds", None))
        if kind == "H":
            rep.h_hotspots, rep.h_map = spots, fmap
        else:
            rep.e_hotspots, rep.e_map = spots, fmap
    if sources.h and len(freqs):
        m_total = np.zeros((len(freqs), 3), complex)
        by_source = []
        for s in sources.h:
            w = s.weights_at(freqs)
            m_k = np.zeros((len(freqs), 3), complex)
            for name, vec in s.moments().items():
                m_k += w[name][:, None] * vec[None, :]
            m_k *= 2 * s.current.c[:, None]
            by_source.append(np.linalg.norm(m_k, axis=1))
            m_total += m_k
        amp = np.linalg.norm(m_total, axis=1)                       # A·m², peak
        e_far = 1.316e-14 * freqs ** 2 * amp / distance_m
        dbuv = 20 * np.log10(np.maximum(e_far, 1e-30) / 1e-6)
        rep.radiated_f, rep.radiated_dbuv = freqs.copy(), dbuv
        rep.limit_dbuv = cispr_limit(freqs)
        band = np.isfinite(rep.limit_dbuv)
        pick = np.flatnonzero(band) if band.any() else np.arange(len(freqs))
        margin = dbuv[pick] - np.nan_to_num(rep.limit_dbuv[pick], nan=0.0)
        k = int(pick[np.argmax(margin)])
        rep.worst_f_hz, rep.worst_dbuv = float(freqs[k]), float(dbuv[k])
        rep.worst_limit_dbuv = float(rep.limit_dbuv[k])
        rep.worst_source = sources.h[int(np.argmax([b[k] for b in by_source]))].label
        if not band.any():
            rep.notes.append("No harmonic reaches the 30 MHz radiated band")
    rep.notes.append("The radiated estimate counts the loops' differential-mode currents "
                     "on the bare board; cables, common-mode currents and an enclosure "
                     "are not modelled")
    return rep


# -------------------------------------------------------------------- building sources
def _patches(g, cell_mm: float) -> np.ndarray:
    """Points (K, 2) a ``cell_mm`` grid apart inside the copper ``g``."""
    import shapely
    x0, y0, x1, y1 = g.bounds
    xs = np.arange(x0 + cell_mm / 2, x1, cell_mm)
    ys = np.arange(y0 + cell_mm / 2, y1, cell_mm)
    if not len(xs) or not len(ys):
        return np.array([[(x0 + x1) / 2, (y0 + y1) / 2]])
    gx, gy = np.meshgrid(xs, ys)
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    inside = pts[shapely.contains_xy(g, pts[:, 0], pts[:, 1])]
    return inside if len(inside) else np.array([[(x0 + x1) / 2, (y0 + y1) / 2]])


def switch_node_source(snap, board, net: str, phase: int, voltage: Spectrum
                       ) -> Optional[ESource]:
    """The switch node's copper, on every layer, as charged patches."""
    from kipi.ac.solver.physics import EPS0_F_PER_MM
    xyz, q, img, cells = [], [], [], []
    for lid in board.order:
        g = board.copper(net, lid)
        if g is None or g.is_empty:
            continue
        cell = float(np.clip(math.sqrt(g.area) / 40, 0.2, 1.0))
        cells.append(cell)
        lay = board.stack.layer(lid)
        for x, y in _patches(g, cell):
            z_face, _, pid = board.plane_under(lid, (x, y))
            if pid is None:
                h, er, z_face = 1.0, 1.0, math.nan
            else:
                h, er = board.gap_er(*((lid, pid) if z_face < lay.z_mid_mm else (pid, lid)))
            xyz.append((x, y, lay.z_mid_mm))
            q.append(EPS0_F_PER_MM * er * cell * cell / h)             # F
            img.append(z_face)
    if not xyz:
        return None
    return ESource(f"{net} (phase {phase})", f"{net} switch node", phase, np.array(xyz, float),
                   np.array(q, float), np.array(img, float), max(cells), voltage)


def field_sources(snap, setup, agg, layouts, wf) -> FieldSources:
    """Every field source of a solve: each loop of :func:`loop_sources` with its current,
    and each phase's switch node with its voltage."""
    from kipi.noise.extract.inductive import loop_sources
    from kipi.noise.parasitics import _Board
    board = _Board(snap, setup.ac.default_er)
    stack = snap.stackup
    out = FieldSources(fsw_hz=wf.fsw_hz,
                       z_top_mm=stack.copper[0].z_top_mm if stack.copper else 0.0,
                       z_bottom_mm=stack.copper[-1].z_bottom_mm if stack.copper else 0.0)
    ring = [xy for poly in snap.outline or [] for xy in poly.outline]
    if ring:
        a = np.asarray(ring, float)
        out.board_bounds = (float(a[:, 0].min()), float(a[:, 1].min()),
                            float(a[:, 0].max()), float(a[:, 1].max()))
    for src in loop_sources(layouts, wf):
        phase = int(src.label[src.label.rfind("phase ") + 6:-1]) if "(phase " in src.label else 0
        lay = stack.layer(src.image_layer) if src.image_layer is not None else None
        plane = (lay.z_bottom_mm, lay.z_top_mm) if lay is not None else None
        a = np.array([s.a for s in src.segments], float).reshape(-1, 3)
        b = np.array([s.b for s in src.segments], float).reshape(-1, 3)
        radius = np.array([s.radius for s in src.segments], float)
        name = src.label.split(" (phase")[0]
        mean = 0.0
        if name.endswith(" field") and 0 < phase <= len(wf.phases):
            mean = wf.phases[phase - 1].inductor.mean
        cur = Spectrum(f"I({name})", "A", mean, np.asarray(src.current, complex))
        out.h.append(HSource(src.label, name, phase, a, b, src.weight(), radius, plane,
                             lay.thickness_mm if lay is not None else 0.035,
                             src.size_mm or _size(a), cur))
    for i, (ph, pw) in enumerate(zip(agg.phases, wf.phases), 1):
        if not ph.switch_net:
            continue
        v = Spectrum(pw.switch_node.name, "V", pw.switch_node.mean, pw.switch_node.c)
        src = switch_node_source(snap, board, ph.switch_net, i, v)
        if src is not None:
            out.e.append(src)
    return out


def _size(a: np.ndarray) -> float:
    if len(a) < 2:
        return 1.0
    span = a.max(axis=0) - a.min(axis=0)
    return max(float(math.sqrt(max(span[0] * span[1], 0.0))), 1.0)
