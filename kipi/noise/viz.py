"""What the 3D view draws of a Coupled Noise result (#152, #178).

**Coupling.** The solve gives each segment of a victim's traces its part of the
receiver's peak noise, by mechanism (:attr:`kipi.noise.result.VictimResult.segment_v`).
Here those parts are colored along the traces, the common-ground part at the driver.

**Hot loops.** Each capacitor's current loop, as the solve traced it through the copper
(or drew it straight between the pads): its path, in the direction the current flows out
of the capacitor's positive pad, in a colour of its own, with the capacitor's name at the
capacitor.

**Fields** (#146). The H field of chosen current loops (the capacitors' and the
inductor's) or the E field of chosen switch nodes on a slice over the board
(:mod:`kipi.noise.fields`): each point's peak over a switching period, coloured on a
log scale, with arrows along the field there; or frames over one period to animate.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

# The coupling view's choices: key, text.
VIEWS = (("off", "Off"), ("total", "Superposition"), ("capacitive", "Capacitive"),
         ("inductive", "Inductive"), ("common", "Common ground"))
_MECH = {"capacitive": ("capacitive",), "inductive": ("inductive",),
         "total": ("capacitive", "inductive"), "common": ()}
# The hot loops view: off, every loop, or one loop by its key in NoiseResult.loop_paths.
LOOPS_OFF, LOOPS_ALL = "off", "all"
# One colour per loop, in the order the loops come, round again after the last.
LOOP_COLORS = ("#ff9a1f", "#2ad4ff", "#8cff3a", "#ff5ec8", "#ffe14d", "#b48bff", "#ff6b4d",
               "#5effc3")
# How far apart along a loop its direction arrows sit, mm.
ARROW_SPACING_MM = 2.0
# The field view: kinds, the slice's default height over the top copper, the animation.
FIELD_KINDS = (("off", "Off"), ("H", "H field (magnetic)"), ("E", "E field (electric)"))
FIELD_HEIGHT_MM = 1.0
FIELD_FRAMES = 96
FIELD_FRAME_MS = 40
FIELD_DECADES = 3.0                     # the colour scale's range under the peak


@dataclass
class CouplingOverlay:
    """What the 3D view draws of a victim's coupling: segments and points colored by their
    part of the peak (mV, on a range of ±limit_mv). Coordinates are (x, y, z) mm, KiCad's."""
    title: str
    segments: np.ndarray                    # (S, 2, 3)
    segment_mv: np.ndarray                  # (S,)
    points: np.ndarray                      # (P, 3)
    point_mv: np.ndarray                    # (P,)
    limit_mv: float = 1.0
    note: str = ""


@dataclass
class LoopPath:
    """One capacitor's loop to draw: its segments (N, 2, 3), each along the current, the
    points to put direction arrows at with the unit direction there, its colour, and the
    name to put at ``label_xyz`` (the capacitor)."""
    key: str                                # as in NoiseResult.loop_paths
    label: str                              # the capacitor's reference
    segments: np.ndarray
    arrow_xyz: np.ndarray                   # (A, 3)
    arrow_dir: np.ndarray                   # (A, 3)
    color: str
    label_xyz: np.ndarray                   # (3,)


@dataclass
class LoopOverlay:
    """The hot loops the 3D view draws (#178)."""
    loops: List[LoopPath] = field(default_factory=list)
    note: str = ""


@dataclass
class FieldOverlay:
    """A field on a slice for the 3D view: the grid's points (P, 3) in KiCad's (x, y, z)
    mm, row by row (``ny`` rows of ``nx``), each point's peak |field| over a period and
    the field there then; ``frames`` (T, P, 3), when animating, the field over one
    period, a frame every ``frame_ms``."""
    title: str
    unit: str
    points: np.ndarray
    nx: int
    ny: int
    magnitude: np.ndarray                   # (P,)
    vectors: np.ndarray                     # (P, 3)
    limits: Tuple[float, float]             # the colour scale, log
    frames: Optional[np.ndarray] = None
    frame_ms: int = FIELD_FRAME_MS
    note: str = ""


def field_choices(result, kind: str) -> List[tuple]:
    """The sources of ``kind`` ("H" or "E") a result has: (key, label)."""
    fs = getattr(result, "fields", None) if result is not None else None
    if fs is None or kind not in ("H", "E"):
        return []
    phases = len(getattr(result.waveforms, "phases", []) or [])
    return [(s.key, f"{s.label} (phase {s.phase})" if phases > 1 and s.phase else s.label)
            for s in fs.of_kind(kind)]


class FieldViews:
    """A result's field slices, worked out once: per kind and height, every source's maps
    on one grid over all of that kind's sources, so any choice of them is only their sum
    (:meth:`FieldSlice.part`); and the last few views put together. The solve works out
    the default height's after the victims; another height is worked out when first asked
    for. Not saved with the result: an opened one works them out again."""

    def __init__(self, result):
        self.result = result
        self._slices = {}                   # (kind, height) -> FieldSlice of all sources
        self._views = OrderedDict()         # field_overlay's arguments -> FieldOverlay
        self._lock = threading.Lock()

    @staticmethod
    def _key(kind, height_mm):
        return kind, round(float(height_mm), 6)

    def ready(self, kind: str, height_mm: float) -> bool:
        return self._key(kind, height_mm) in self._slices

    def slice(self, kind: str, height_mm: float):
        """Every ``kind`` source's maps ``height_mm`` over the top copper (None: none)."""
        from kipi.noise.fields import FieldSlice
        fs = self.result.fields
        if not fs.of_kind(kind):
            return None
        with self._lock:                    # the solve and the panel may both ask
            k = self._key(kind, height_mm)
            if k not in self._slices:
                self._slices[k] = FieldSlice(fs, kind, fs.of_kind(kind), fs.z_top_mm + height_mm,
                                             self.result.waveforms.samples_per_period())
            return self._slices[k]

    def prepare(self, height_mm: float = FIELD_HEIGHT_MM, check=None) -> None:
        """Work out both kinds' slices at ``height_mm`` (``check`` between them may cancel)."""
        for kind in ("H", "E"):
            if check is not None:
                check()
            self.slice(kind, height_mm)

    def _view_key(self, kind, keys, height_mm, animate):
        return (*self._key(kind, height_mm), tuple(keys), bool(animate))

    def cached(self, kind: str, keys: Sequence[str], height_mm: float, animate: bool):
        """The view put together already, or None."""
        return self._views.get(self._view_key(kind, keys, height_mm, animate))

    def overlay(self, kind: str, keys: Sequence[str], height_mm: float, animate: bool):
        k = self._view_key(kind, keys, height_mm, animate)
        if k in self._views:
            self._views.move_to_end(k)
            return self._views[k]
        ov = _compose(self.result, kind, keys, height_mm, animate, self)
        self._views[k] = ov
        while len(self._views) > _VIEWS_KEPT:
            self._views.popitem(last=False)
        return ov


_VIEWS_KEPT = 6                         # an animation's frames are tens of megabytes
_VIEWS_LOCK = threading.Lock()


def field_views(result) -> Optional[FieldViews]:
    """``result``'s :class:`FieldViews`, made on first asking; None without fields."""
    if result is None or getattr(result, "fields", None) is None:
        return None
    with _VIEWS_LOCK:
        views = getattr(result, "field_views", None)
        if views is None:
            views = result.field_views = FieldViews(result)
        return views


def field_overlay(result, kind: str, keys: Sequence[str], height_mm: float = FIELD_HEIGHT_MM,
                  animate: bool = False) -> Optional[FieldOverlay]:
    """The field of the sources ``keys`` of ``kind`` on a slice ``height_mm`` over the
    top copper, or None (off, no such sources, or a result without fields)."""
    views = field_views(result)
    if views is None or kind not in ("H", "E"):
        return None
    return views.overlay(kind, keys, height_mm, animate)


def _compose(result, kind, keys, height_mm, animate, views) -> Optional[FieldOverlay]:
    fs = result.fields
    chosen = [s for s in fs.of_kind(kind) if s.key in set(keys)]
    if not chosen:
        return None
    sl = views.slice(kind, height_mm).part(chosen)
    n_t = result.waveforms.samples_per_period()
    stride = max(1, n_t // 1024)
    mag, vec = sl.peak(stride)
    top = float(mag.max()) if mag.size and mag.max() > 0 else 1.0
    limits = (top / 10 ** FIELD_DECADES, top)
    frames = sl.frames(FIELD_FRAMES) if animate else None
    i = int(np.argmax(mag))
    what = "H" if kind == "H" else "E"
    names = ", ".join(s.label for s in chosen)
    note = (f"{what} field of {names}, {height_mm:g} mm over the top copper: each point's "
            f"peak over a period, up to {top:.3g} {sl.unit} at ({sl.pts[i, 0]:.1f}, "
            f"{sl.pts[i, 1]:.1f}) mm" + ("; animating one period" if animate else ""))
    return FieldOverlay(f"{what} field, peak over a period ({sl.unit})", sl.unit, sl.pts,
                        sl.nx, sl.ny, mag, vec, limits, frames, FIELD_FRAME_MS, note)


def coupling_overlay(result, victim: str, view: str) -> Optional[CouplingOverlay]:
    """The overlay of ``victim``'s coupling in ``view`` (a :data:`VIEWS` key), or None
    (view off, no such victim, or a result saved before #152)."""
    if result is None or view not in _MECH:
        return None
    v = next((x for x in getattr(result, "victims", []) if x.name == victim), None)
    if v is None or v.segments_xyz is None or not len(v.segments_xyz):
        return None
    mechs = [m for m in _MECH[view] if m in v.segment_v]
    if mechs:
        seg_mv = 1e3 * sum(v.segment_v[m] for m in mechs)
        segments = v.segments_xyz
    else:
        seg_mv, segments = np.zeros(0), np.zeros((0, 2, 3))
    common = view in ("total", "common") and v.driver_xyz is not None and v.common_v
    points = np.array([v.driver_xyz]) if common else np.zeros((0, 3))
    point_mv = np.array([1e3 * v.common_v]) if common else np.zeros(0)
    shown = np.concatenate([np.abs(seg_mv), np.abs(point_mv)])
    limit = float(shown.max()) if len(shown) and shown.max() > 0 else 1.0
    name = dict(VIEWS)[view].lower()
    part = sum(seg_mv) + sum(point_mv)
    note = (f"{v.name}: {part:.3g} mV of its {v.peak_v * 1e3:.4g} mV peak is {name} "
            f"coupling, at {v.peak_t_s * 1e9:.4g} ns into the period")
    if view == "total":
        note = (f"{v.name}: {v.peak_v * 1e3:.4g} mV peak at {v.peak_t_s * 1e9:.4g} ns into "
                f"the period; red adds to it, blue takes from it")
    if view == "common" and not common:
        note = f"{v.name}: no common-ground coupling (it needs the board model)"
    return CouplingOverlay(f"{dict(VIEWS)[view]} share of the peak (mV)", segments, seg_mv,
                           points, point_mv, limit, note)


def loop_choices(result) -> List[tuple]:
    """The hot loops view's choices for a result: (key, text), off and all first."""
    keys = list(getattr(result, "loop_paths", {}) or {}) if result is not None else []
    if not keys:
        return []
    return [(LOOPS_OFF, "Off"), (LOOPS_ALL, "All capacitors")] + [(k, k) for k in keys]


def _arrows(segments: np.ndarray):
    """Points every ARROW_SPACING_MM along the segments (at least one, in the middle of
    the longest run), and the unit direction of the segment each sits on."""
    d = segments[:, 1] - segments[:, 0]
    length = np.linalg.norm(d, axis=1)
    pts, dirs = [], []
    walked = ARROW_SPACING_MM / 2
    for (a, b), ln in zip(segments, length):
        if ln <= 0:
            continue
        u = (b - a) / ln
        while walked <= ln:
            pts.append(a + u * walked)
            dirs.append(u)
            walked += ARROW_SPACING_MM
        walked -= ln
    if not pts and length.max() > 0:
        i = int(np.argmax(length))
        pts.append(segments[i].mean(axis=0))
        dirs.append(d[i] / length[i])
    return np.array(pts).reshape(-1, 3), np.array(dirs).reshape(-1, 3)


def hot_loops_overlay(result, which: str) -> Optional[LoopOverlay]:
    """The hot loops to draw: every one (``which`` = LOOPS_ALL), the one keyed ``which``,
    or None (off, nothing traced, or a result saved before the loops were kept)."""
    if result is None or which == LOOPS_OFF:
        return None
    paths = getattr(result, "loop_paths", {}) or {}
    keys = list(paths) if which == LOOPS_ALL else [which] if which in paths else []
    if not keys:
        return None
    reports = {h.phase: h for h in getattr(result, "hot_loops", [])}
    loops, notes = [], []
    for i, key in enumerate(list(paths)):
        if key not in keys:
            continue
        segs = np.asarray(paths[key], float).reshape(-1, 2, 3)
        if not len(segs):
            continue
        ref = key.split(" (phase")[0]
        phase = int(key[key.rfind("phase ") + 6:-1]) if "(phase " in key else 0
        arrow_xyz, arrow_dir = _arrows(segs)
        # The capacitor sits where the loop's last segment ends (the current comes back
        # into its positive pad): name it there.
        loops.append(LoopPath(key, ref, segs, arrow_xyz, arrow_dir,
                              LOOP_COLORS[i % len(LOOP_COLORS)], segs[-1, 1].copy()))
        h = reports.get(phase)
        if h is not None and ref in getattr(h, "cap_loops_h", {}):
            how = "through the copper" if getattr(h, "traced", False) else "straight"
            notes.append(f"{key}: {h.cap_loops_h[ref] * 1e9:.3g} nH, {how}")
        else:
            notes.append(key)
    if not loops:
        return None
    lead = ("Each capacitor's ripple current, from its positive pad round the loop and back "
            "to its ground pad: ")
    return LoopOverlay(loops, lead + "; ".join(notes))
