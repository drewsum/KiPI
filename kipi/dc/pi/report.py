"""Tables, exports and saved results for a solved power tree (no Qt).

* :func:`via_rows`   every via / PTH barrel across all groups, highest current first
* :func:`loss_rows`  copper loss per group, split by layer, vias and DC connections
* :func:`pin_rows`   every solved pin of every group: its voltage and current
* :func:`layer_temps` the hottest copper per layer after an electro-thermal solve
* :func:`export_csv` loads, VRMs, DC connections, pins, vias, losses and copper
  temperature as CSV files
* :func:`export_vtk` each group's 3D results as a ParaView multiblock (.vtm)
* :func:`save_results` / :func:`load_results`  reopen a solve without re-solving
"""

from __future__ import annotations

import csv
import gzip
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from kipi import __version__, compat
from kipi.core import results_io
from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter
from kipi.core.temperature import at as temperature_at
from kipi.dc.pi.tree import drop_or_boost

RESULTS_FORMAT = 2          # 2: JSON and arrays (#99); 1: a gzipped pickle
# The name says what was solved; files before 0.8.0 were all .kipi-results.
RESULTS_SUFFIX = ".kipi-dc-results"
THERMAL_RESULTS_SUFFIX = ".kipi-dc-thermal-results"
LEGACY_RESULTS_SUFFIX = ".kipi-results"
OPEN_FILTER = (f"KiPI DC results (*{RESULTS_SUFFIX} *{THERMAL_RESULTS_SUFFIX} "
               f"*{LEGACY_RESULTS_SUFFIX})")


def results_suffix(tree) -> str:
    """The file suffix for ``tree``: DC, or DC with an electro-thermal solve."""
    return THERMAL_RESULTS_SUFFIX if tree.thermal is not None else RESULTS_SUFFIX


# ---------------------------------------------------------------------------
# Vias
# ---------------------------------------------------------------------------

@dataclass
class ViaRow:
    group: str
    owner_id: str
    kind: str                 # "via" or "pth"
    label: str                # "via" or the pad label (e.g. "J601.2")
    x: float
    y: float
    drill_mm: float
    current: float            # A, the largest through any of its barrel segments
    power: float              # W in the barrel
    passed: Optional[bool]    # vs the limit, if one was given


def via_rows(tree, snap: BoardSnapshot, limit_a: Optional[float] = None) -> List[ViaRow]:
    pads = {p.id: p for p in snap.pads}
    rows = []
    for g in tree.groups.values():
        for v in g.result.fields.vias:
            label = pads[v.owner_id].label if v.kind == "pth" and v.owner_id in pads else "via"
            passed = None if limit_a is None else v.current <= limit_a
            rows.append(ViaRow(g.name, v.owner_id, v.kind, label, v.position[0], v.position[1],
                               v.drill_mm, v.current, v.power, passed))
    rows.sort(key=lambda r: -r.current)
    return rows


# ---------------------------------------------------------------------------
# Copper temperature
# ---------------------------------------------------------------------------

@dataclass
class LayerTempRow:
    layer: str
    t_max: float              # degC, the hottest solved copper on the layer
    rise: float               # above ambient
    x: float                  # where, KiCad mm
    y: float
    group: str                # the net group that copper belongs to
    passed: Optional[bool]    # vs the limits, if any were given


def layer_temps(tree, snap: BoardSnapshot, limits=None) -> List[LayerTempRow]:
    """The hottest solved copper on each layer, from an electro-thermal solve (empty
    without one). ``limits`` is a ThermalSettings whose max temperature / rise grade
    each layer; the rise is taken over the ambient the solve used."""
    th = getattr(tree, "thermal", None)
    if th is None:
        return []
    T = th.sampler()             # each layer's own copper (kipi.core.temperature)
    ambient = th.settings.ambient_c
    best: Dict[int, tuple] = {}             # layer id -> (t, x, y, group)
    for g in tree.groups.values():
        mesh = g.result.mesh
        for lid in np.unique(mesh.tri_layer):
            nodes = np.unique(mesh.tris[mesh.tri_layer == lid])
            pts = mesh.points[nodes]
            t = np.broadcast_to(temperature_at(T, pts, int(lid)), (len(pts),))
            k = int(np.argmax(t))
            if lid not in best or t[k] > best[lid][0]:
                best[lid] = (float(t[k]), float(pts[k, 0]), float(pts[k, 1]), g.name)
    rows = []
    for cu in snap.stackup.copper:          # top to bottom
        if cu.id in best:
            t, x, y, group = best[cu.id]
            rows.append(LayerTempRow(cu.name, t, t - ambient, x, y, group,
                                     None if limits is None else limits.temp_passed(t, ambient)))
    return rows


def thermal_passed(rows: List[LayerTempRow]) -> Optional[bool]:
    """FAIL if any layer is over a limit; None when nothing was judged."""
    judged = [r.passed for r in rows if r.passed is not None]
    return all(judged) if judged else None


def limits_text(thermal) -> str:
    """The copper temperature limits in words, or "" without any."""
    out = []
    if thermal.max_temp_c is not None:
        out.append(f"copper at most {thermal.max_temp_c:g} °C")
    if thermal.max_rise_c is not None:
        out.append(f"at most {thermal.max_rise_c:g} °C above ambient")
    return ", ".join(out)


# ---------------------------------------------------------------------------
# Losses
# ---------------------------------------------------------------------------

@dataclass
class LossRow:
    group: str
    by_layer: Dict[str, float]          # layer name -> W in that copper layer
    vias: float                         # W in via / PTH barrels
    links: float                        # W in DC connections (their own resistance)
    total: float = field(init=False)

    def __post_init__(self):
        self.total = sum(self.by_layer.values()) + self.vias + self.links


def loss_rows(tree, snap: BoardSnapshot) -> List[LossRow]:
    st = snap.stackup
    rows = []
    for g in tree.groups.values():
        f = g.result.fields
        tri_layer = g.result.mesh.tri_layer
        by_layer = {}
        for cu in st.copper:
            sel = tri_layer == cu.id
            if np.any(sel):
                by_layer[cu.name] = float(f.tri_power[sel].sum())
        rows.append(LossRow(g.name, by_layer, float(f.summary["power_barrels"]),
                            float(f.summary.get("power_series", 0.0))))
    return rows


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------

PIN_CURRENT_SOLVED = "solved"
PIN_CURRENT_SHARED = "equal share"


@dataclass
class PinRow:
    group: str
    kind: str                 # the group's: "rail" or "ground"
    terminal: str             # e.g. "U1 (output)", "U2", "U2 [VDD]", "R5"
    role: str                 # "source", "load" or "DC connection"
    part: str
    pin: str
    x: float
    y: float
    v: float
    nominal: float            # the group's reference: its VRM's set point, or 0 V
    current: Optional[float]  # A drawn from the copper at the pin (negative: delivered)
    current_is: str           # PIN_CURRENT_SOLVED or PIN_CURRENT_SHARED; "" if unknown

    @property
    def deviation(self) -> float:
        """Nominal minus the pin's voltage, volts (negative: a boost)."""
        return self.nominal - self.v


def pin_rows(tree, snap: BoardSnapshot) -> List[PinRow]:
    """Every pin each group was solved with (#71): VRM outputs, phases, inputs and sense
    pins, load supply and ground pins, and the pins of DC connections. A source's (VRM
    output's) pin currents are solved; a load's current is shared equally by its pins, as
    the solver applies it, and so is a DC connection's (its pins on a side are one node).
    Results saved before 0.7.2 kept no pins and give none."""
    by_id = {p.id: p for p in snap.pads}
    rows = []

    def add(g, terminal, role, pids, vs, amps, how):
        for pid, v, a in zip(pids, vs, amps):
            p = by_id.get(pid)
            if p is None:
                continue
            ok = a is not None and not (isinstance(a, float) and math.isnan(a))
            rows.append(PinRow(g.name, g.kind, terminal, role, p.footprint, p.number,
                               p.position[0], p.position[1], v, g.result.fields.v_ref,
                               a if ok else None, how if ok else ""))

    for g in tree.groups.values():
        f = g.result.fields
        for t in sorted(f.terminals, key=lambda t: t.kind != "source"):
            pids = getattr(t, "pad_ids", [])
            if not pids:
                continue
            add(g, t.name, t.kind, pids, t.v_pads, t.i_pads,
                PIN_CURRENT_SOLVED if t.i_solved else PIN_CURRENT_SHARED)
        for s in f.series:
            for pids, v, sign in ((getattr(s, "pads_a", []), s.v_a, 1.0),
                                  (getattr(s, "pads_b", []), s.v_b, -1.0)):
                if pids:
                    add(g, s.name, "DC connection", pids, [v] * len(pids),
                        [sign * s.current / len(pids)] * len(pids), PIN_CURRENT_SHARED)
    return rows


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def _write(path: Path, header: List[str], rows: List[list]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows([[_fmt(x) for x in row] for row in rows])
    return path


def _fmt(x):
    """CSV cell: blanks for missing values, floats to 9 significant digits."""
    if x is None:
        return ""
    if isinstance(x, (float, np.floating)):
        return "" if math.isnan(x) else f"{float(x):.9g}"
    return x


def export_csv(tree, snap: BoardSnapshot, folder, stem: str = "kipi",
               via_limit_a: Optional[float] = None, thermal_limits=None) -> List[Path]:
    """``thermal_limits``: a ThermalSettings whose temperature limits grade the copper."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    # Deviations from nominal are written as a positive magnitude plus "drop" (below
    # nominal) or "boost" (above it).
    def dev(dv, pct):
        mag, kind = drop_or_boost(dv)
        return [mag * 1e3, None if pct is None else abs(pct), kind]

    out.append(_write(folder / f"{stem}_loads.csv",
                      ["load", "group", "current_A", "vrm_set_V", "load_nominal_V",
                       "v_supply_mean_V",
                       "v_supply_min_V", "v_ground_mean_V", "v_effective_V", "v_lowest_pin_V",
                       "v_highest_pin_V", "mean_mV", "mean_pct", "mean_is", "worst_pin_V",
                       "worst_pin_mV", "worst_pin_pct", "worst_pin_is", "tolerance",
                       "tolerance_min_V", "tolerance_max_V", "result"],
                      [[l.name, l.group, l.current, l.nominal, l.load_nominal, l.v_supply,
                        l.v_supply_min,
                        l.v_ground, l.v_effective, l.v_low, l.v_high,
                        *dev(l.drop, l.drop_pct), l.v_worst,
                        *dev(l.drop_worst, l.drop_worst_pct), l.tolerance, l.limit_low,
                        l.limit_high, l.verdict]
                       for l in tree.loads]))
    out.append(_write(folder / f"{stem}_load_pin_groups.csv",
                      ["load", "pin_group", "current_A", "v_mean_V", "v_min_V", "v_max_V"],
                      [[l.name, n, a, vm, vmin, vmax] for l in tree.loads
                       for n, a, vm, vmin, vmax in l.pin_groups]))
    out.append(_write(folder / f"{stem}_vrms.csv",
                      ["vrm", "type", "group", "v_set_V", "load_line_mOhm", "v_target_V",
                       "v_out_V", "v_sense_V",
                       "v_ground_sense_V", "i_out_A", "v_in_V", "i_in_A", "p_out_W", "p_in_W",
                       "p_loss_W", "i_limit_A", "result"],
                      [[v.name, v.kind, v.group, v.v_set, v.load_line_mohm, v.v_target,
                        v.v_out, v.v_sense, v.v_ground_sense,
                        v.i_out, v.v_in, v.i_in, v.p_out, v.p_in, v.p_loss, v.current_limit_a,
                        "" if v.passed is None else ("PASS" if v.passed else "FAIL")]
                       for v in tree.vrms]))
    if any(v.phases for v in tree.vrms):
        out.append(_write(folder / f"{stem}_vrm_phases.csv",
                          ["vrm", "phase", "v_out_V", "i_out_A", "v_in_V", "i_in_A", "p_out_W",
                           "p_in_W", "p_loss_W"],
                          [[v.name, p.index, p.v_out, p.i_out, p.v_in, p.i_in, p.p_out, p.p_in,
                            p.p_loss] for v in tree.vrms for p in v.phases]))
    out.append(_write(folder / f"{stem}_dc_connections.csv",
                      ["part", "group", "resistance_ohm", "current_A", "v_a_V", "v_b_V",
                       "power_W"],
                      [[s.name, g.name, s.resistance, s.current, s.v_a, s.v_b, s.power]
                       for g in tree.groups.values() for s in g.result.fields.series]))
    out.append(_write(folder / f"{stem}_pins.csv",
                      ["group", "group_kind", "terminal", "role", "part", "pin", "x_mm", "y_mm",
                       "v_V", "nominal_V", "deviation_mV", "deviation_pct", "deviation_is",
                       "current_A", "current_is"],
                      [[r.group, r.kind, r.terminal, r.role, r.part, r.pin, r.x, r.y, r.v,
                        r.nominal,
                        *dev(r.deviation,
                             100.0 * r.deviation / abs(r.nominal) if r.nominal else None),
                        r.current, r.current_is]
                       for r in pin_rows(tree, snap)]))
    out.append(_write(folder / f"{stem}_vias.csv",
                      ["group", "kind", "label", "x_mm", "y_mm", "drill_mm", "current_A",
                       "power_W", "limit_A", "result"],
                      [[r.group, r.kind, r.label, r.x, r.y, r.drill_mm, r.current, r.power,
                        via_limit_a,
                        "" if r.passed is None else ("PASS" if r.passed else "FAIL")]
                       for r in via_rows(tree, snap, via_limit_a)]))
    losses = loss_rows(tree, snap)
    layers = [c.name for c in snap.stackup.copper]
    out.append(_write(folder / f"{stem}_losses.csv",
                      ["group", *[f"{n}_W" for n in layers], "vias_W", "dc_connections_W",
                       "total_W"],
                      [[r.group, *[r.by_layer.get(n, 0.0) for n in layers], r.vias, r.links,
                        r.total] for r in losses]))
    temps = layer_temps(tree, snap, thermal_limits)
    if temps:
        lim = thermal_limits
        out.append(_write(folder / f"{stem}_copper_temperature.csv",
                          ["layer", "t_max_C", "rise_C", "x_mm", "y_mm", "group",
                           "limit_C", "rise_limit_C", "result"],
                          [[r.layer, r.t_max, r.rise, r.x, r.y, r.group,
                            lim and lim.max_temp_c, lim and lim.max_rise_c,
                            "" if r.passed is None else ("PASS" if r.passed else "FAIL")]
                           for r in temps]))
    return out


# ---------------------------------------------------------------------------
# ParaView
# ---------------------------------------------------------------------------

def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]+", "_", name).strip("_") or "group"


def export_vtk(tree, scenes: Dict[str, object], snap: BoardSnapshot, folder,
               stem: str = "kipi") -> List[Path]:
    """One .vtm per group: a block per copper layer (point V; cell J, P) and the vias
    (cell I_mA). Coordinates are mm with y flipped as in the viewer."""
    import pyvista as pv
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    names = {c.id: c.name for c in snap.stackup.copper}
    out = []
    for name, scene in scenes.items():
        mb = pv.MultiBlock()
        for lid, lr in scene.layers.items():
            mb[names.get(lid, str(lid))] = lr.mesh
        if scene.vias is not None:
            mb["vias"] = scene.vias
        path = folder / f"{stem}_{_safe(name)}.vtm"
        mb.save(str(path))
        out.append(path)
    return out


# ---------------------------------------------------------------------------
# Saved results
# ---------------------------------------------------------------------------

def save_results(path, tree, snap: BoardSnapshot, setup) -> Path:
    """Write a results file: JSON and NumPy arrays in a zip (:mod:`kipi.core.results_io`),
    which opens on any machine and runs no code. The 3D views are not saved: they are
    rebuilt from the solution, with the board's stackup saved alongside, when it opens."""
    header = {"format": RESULTS_FORMAT, "kind": "dc", "kipi": __version__,
              "saved": time.strftime("%Y-%m-%d %H:%M:%S"), "board": snap.board_name,
              "setup": setup.to_dict() if setup is not None else None}
    return results_io.write(path, header, {"tree": tree, "stackup": snap.stackup})


def load_results(path, progress: ProgressReporter = NULL, legacy: bool = False) -> dict:
    """``{"tree", "scenes", "setup", "board", "saved", ...}`` from a results file.

    Files saved before 0.8.0 are pickles, which can run code when they are opened; they are
    read only with ``legacy=True`` (``"legacy"`` is then True in the result), else
    :class:`~kipi.core.results_io.LegacyResults` is raised."""
    if results_io.is_legacy(path):
        if not legacy:
            raise results_io.LegacyResults("Results saved by KiPI 0.7 or earlier")
        with gzip.open(Path(path), "rb") as fh:
            # Finds classes that moved since the file was saved (see kipi.compat).
            payload = compat.Unpickler(fh).load()
        if not isinstance(payload, dict) or payload.get("format", 0) > 1:
            raise ValueError("Not a KiPI results file")
        payload["legacy"] = True
    else:
        payload = results_io.read(path)
        if payload.get("format", 0) > RESULTS_FORMAT:
            raise ValueError("These results are from a newer KiPI")
    kind = payload.get("kind", "dc")                # files before 0.7.1 have no kind
    if kind != "dc":
        mode = "Coupled Noise" if kind == "noise" else "AC"
        raise ValueError(f"This holds {mode} results: open it in {mode} mode")
    if not payload.get("legacy"):
        from kipi.dc.pi.model import PISetup
        data = payload.pop("data")
        payload["tree"] = data["tree"]
        payload["setup"] = PISetup.from_dict(payload["setup"]) if payload.get("setup") else None
        with progress.timed("Building result views"):
            from kipi.dc.viz.results import build_scenes
            payload["scenes"] = build_scenes(payload["tree"], data["stackup"], progress)
    return payload
