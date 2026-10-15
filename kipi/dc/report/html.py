"""Self-contained HTML report for a solved power tree.

One ``.html`` file with:
* a summary and the load / VRM / DC connection / pin / loss / via tables,
* per net group: an interactive 3D view (Plotly, rotate and zoom in the browser), a
  per-layer table and top-down voltage and current-density maps for every layer,
* the board temperature map and iteration history when an electro-thermal solve ran.

Plotly's JavaScript is embedded once, so the report works offline and can be shared
as a single file. Images are rendered with matplotlib's Agg backend (thread safe).
"""

from __future__ import annotations

import base64
import datetime as _dt
import html
import io
import math
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from kipi import __version__
from kipi.core.model.board import BoardSnapshot
from kipi.core.model.stackup import summary as stackup_summary
from kipi.dc.pi.tree import fmt_drop

TRIANGLE_BUDGET = 60_000      # per group's 3D view
Z_EXAGGERATION = 5.0


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _e(x) -> str:
    return html.escape(str(x))


def _num(x, fmt=".4g") -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    return format(x, fmt)


def _table(headers: List[str], rows: List[List[str]], cls: str = "") -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _verdict(passed: Optional[bool], text: str = "") -> str:
    """PASS / FAIL, or ``text`` when the result was not judged or failed for a reason."""
    if passed is None:
        return f'<span class="unjudged">{_e(text)}</span>' if text else ""
    label = _e(text) if text else ("PASS" if passed else "FAIL")
    return f'<span class="pass">{label}</span>' if passed else f'<span class="fail">{label}</span>'


def _png(fig) -> str:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    FigureCanvasAgg(fig)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor="white")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def _layer_map(lr, values, cell: bool, title: str, cmap: str, unit: str,
               clip_pct: Optional[float] = None, outline=None) -> str:
    """Top-down tripcolor of one layer (KiCad y down, drawn the same way as in KiCad)."""
    from matplotlib.figure import Figure
    from matplotlib.tri import Triangulation
    pts, tris = lr.pts2, lr.tris
    fig = Figure(figsize=(5.2, 4.2))
    ax = fig.add_subplot(111)
    tri = Triangulation(pts[:, 0], pts[:, 1], tris)
    vals = np.asarray(values, dtype=float)
    finite = vals[np.isfinite(vals)]
    if len(finite) == 0:
        return ""
    vmin, vmax = float(finite.min()), float(finite.max())
    if clip_pct is not None:
        vmax = float(np.percentile(finite, clip_pct))
    if vmax <= vmin:
        vmax = vmin + 1e-12
    if cell:
        m = ax.tripcolor(tri, facecolors=np.nan_to_num(vals, nan=vmin), cmap=cmap,
                         vmin=vmin, vmax=vmax, shading="flat")
    else:
        m = ax.tripcolor(tri, np.nan_to_num(vals, nan=vmin), cmap=cmap, vmin=vmin, vmax=vmax,
                         shading="gouraud")
    if outline is not None:
        for ring in outline:
            a = np.asarray(ring + ring[:1])
            ax.plot(a[:, 0], a[:, 1], color="#777", lw=0.6)
    ax.set_aspect("equal")
    ax.invert_yaxis()
    ax.set_title(title, fontsize=10)
    ax.tick_params(labelsize=7)
    ax.set_xlabel("x (mm)", fontsize=8)
    ax.set_ylabel("y (mm)", fontsize=8)
    cb = fig.colorbar(m, ax=ax, shrink=0.85)
    cb.set_label(unit, fontsize=8)
    cb.ax.tick_params(labelsize=7)
    return _png(fig)


def _thermal_map(th, outline) -> str:
    from matplotlib.figure import Figure
    fig = Figure(figsize=(6.0, 4.8))
    ax = fig.add_subplot(111)
    ext = [th.x[0], th.x[-1], th.y[-1], th.y[0]]
    m = ax.imshow(th.temps, extent=ext, cmap="inferno", origin="upper",
                  interpolation="bilinear")
    if outline is not None:
        for ring in outline:
            a = np.asarray(ring + ring[:1])
            ax.plot(a[:, 0], a[:, 1], color="#bbb", lw=0.6)
    x, y, t = th.hottest()
    ax.plot([x], [y], marker="+", color="cyan", ms=12)
    ax.set_aspect("equal")
    ax.set_title(f"Board temperature (peak {t:.1f} °C)", fontsize=10)
    ax.set_xlabel("x (mm)", fontsize=8)
    ax.set_ylabel("y (mm)", fontsize=8)
    ax.tick_params(labelsize=7)
    cb = fig.colorbar(m, ax=ax, shrink=0.85)
    cb.set_label("°C", fontsize=8)
    return _png(fig)


def _plotly_3d(scene, snap: BoardSnapshot, div_id: str) -> str:
    """Interactive 3D view of one group: copper colored by voltage, plus vias."""
    import plotly.graph_objects as go
    import plotly.io as pio
    import pyvista as pv

    st = snap.stackup
    total = sum(len(lr.tris) for lr in scene.layers.values()) or 1
    keep = min(1.0, TRIANGLE_BUDGET / total)
    # Color range from the 1st-99th percentile of node voltages: a small high-voltage
    # net (e.g. a regulator's switch node in the same group) would otherwise flatten the
    # planes into one color. Hover still shows exact values.
    allv = np.concatenate([lr.node_v[np.isfinite(lr.node_v)] for lr in scene.layers.values()])
    vmin, vmax = (np.percentile(allv, [1, 99]) if len(allv) else scene.ranges["voltage"])
    vmin, vmax = float(vmin), float(max(vmax, vmin + 1e-9))
    traces = []
    for lid, lr in scene.layers.items():
        cu = st.layer(lid)
        pts = np.column_stack([lr.pts2[:, 0], -lr.pts2[:, 1],
                               np.full(len(lr.pts2), cu.z_top_mm * Z_EXAGGERATION)])
        faces = np.column_stack([np.full(len(lr.tris), 3), lr.tris]).ravel()
        sheet = pv.PolyData(pts, faces)
        sheet.point_data["V"] = np.nan_to_num(lr.node_v, nan=vmin)
        if keep < 0.98 and sheet.n_cells > 200:
            # Quadric decimation of a flat sheet warns "Unable to factor linear system" at
            # nearly every vertex (coplanar quadrics are singular; the result is fine), so
            # silence VTK warnings for this call only.
            from vtkmodules.vtkCommonCore import vtkObject
            was = vtkObject.GetGlobalWarningDisplay()
            vtkObject.GlobalWarningDisplayOff()
            try:
                sheet = sheet.decimate(1.0 - keep, enable_all_attribute_error=True,
                                       volume_preservation=False)
            finally:
                vtkObject.SetGlobalWarningDisplay(was)
        tri = sheet.faces.reshape(-1, 4)[:, 1:]
        p = np.asarray(sheet.points)
        traces.append(go.Mesh3d(
            x=p[:, 0], y=p[:, 1], z=p[:, 2], i=tri[:, 0], j=tri[:, 1], k=tri[:, 2],
            intensity=np.asarray(sheet.point_data["V"]), colorscale="Turbo", cmin=vmin,
            cmax=vmax, name=cu.name, showscale=not traces, flatshading=True,
            colorbar=dict(title="V", len=0.7),
            hovertemplate=(f"{cu.name}<br>x %{{x:.2f}} mm<br>y %{{y:.2f}}"
                           "<br>V %{intensity:.5f}<extra></extra>")))
    if len(scene.via_segments):
        seg = scene.via_segments
        xs, ys, zs = [], [], []
        for (a, b) in seg:
            xs += [a[0], b[0], None]
            ys += [-a[1], -b[1], None]
            zs += [a[2] * Z_EXAGGERATION, b[2] * Z_EXAGGERATION, None]
        traces.append(go.Scatter3d(x=xs, y=ys, z=zs, mode="lines", name="vias",
                                   line=dict(color="#bbbbbb", width=3), hoverinfo="skip"))
    fig = go.Figure(traces)
    fig.update_layout(height=560, margin=dict(l=0, r=0, t=30, b=0),
                      scene=dict(aspectmode="data", xaxis_title="x (mm)",
                                 yaxis_title="y (mm)", zaxis_title=f"z ×{Z_EXAGGERATION:g}"),
                      legend=dict(orientation="h"))
    return pio.to_html(fig, include_plotlyjs=False, full_html=False, div_id=div_id,
                       config={"displaylogo": False})


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

CSS = """
body { font-family: Segoe UI, Helvetica, Arial, sans-serif; margin: 24px auto; max-width: 1180px;
       color: #222; background: #fafafa; }
h1 { margin-bottom: 0; } h2 { border-bottom: 2px solid #ddd; padding-bottom: 4px; margin-top: 36px; }
h3 { margin-top: 26px; }
.meta { color: #666; margin-top: 4px; }
table { border-collapse: collapse; margin: 8px 0 16px; font-size: 13px; background: white; }
th, td { border: 1px solid #ddd; padding: 4px 8px; text-align: right; }
th { background: #f0f0f0; } td:first-child, th:first-child { text-align: left; }
.pass { color: #1a8f2c; font-weight: bold; } .fail { color: #c62828; font-weight: bold; }
.unjudged { color: #b26a00; font-weight: bold; }
.maps { display: flex; flex-wrap: wrap; gap: 12px; }
.maps figure { margin: 0; background: white; border: 1px solid #ddd; padding: 6px; }
.maps img { width: 520px; max-width: 100%; }
.note { color: #666; font-size: 12px; }
nav a { margin-right: 12px; }
details > summary { cursor: pointer; margin: 6px 0; }
"""


def write_report(path, tree, scenes: Dict[str, object], snap: BoardSnapshot, setup,
                 progress=None) -> Path:
    import plotly.offline
    from kipi.dc.pi.report import (
        layer_temps, limits_text, loss_rows, pin_rows, thermal_passed, via_rows,
    )

    def info(t):
        if progress is not None:
            progress.info(t)

    path = Path(path)
    outline = [p.outline for p in snap.outline] if snap.outline else None
    names = {c.id: c.name for c in snap.stackup.copper}
    parts: List[str] = []
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    th = getattr(tree, "thermal", None)

    # ---------------- summary
    parts.append(f"<h1>KiPI power-integrity report</h1><div class='meta'>{_e(snap.board_name)} "
                 f"· {now} · KiPI {__version__} · solved in {tree.solve_s:.1f} s</div>")
    nav = ["<a href='#loads'>Loads</a>", "<a href='#vrms'>VRMs</a>",
           "<a href='#pins'>Pins</a>", "<a href='#losses'>Losses</a>",
           "<a href='#vias'>Vias</a>"]
    if th is not None:
        nav.append("<a href='#thermal'>Thermal</a>")
    nav += [f"<a href='#g{i}'>{_e(n)}</a>" for i, n in enumerate(tree.groups)]
    parts.append("<nav>" + " ".join(nav) + "</nav>")
    s = setup.settings
    parts.append(f"<p class='note'>Mesh: max edge {s.max_edge_mm:g} mm, via grading "
                 f"{s.grading:g}, barrel plating {s.plating_mm * 1000:g} µm, copper "
                 f"resistivity {s.rho_ohm_mm * 1e-3:.4g} Ω·m at 20 °C "
                 f"(TCR {s.tcr_per_c:g}/°C)"
                 + ("; layer overrides: " + ", ".join(
                     f"{_e(n)} {_e(s.conductor_by_layer.get(n, ''))} {r * 1e-3:.4g} Ω·m "
                     f"(TCR {s.tcr_for(n):g}/°C)".replace("  ", " ")
                     for n, r in sorted(s.rho_by_layer.items()))
                    if s.rho_by_layer else "")
                 + ".</p>")
    parts.append(f"<p class='note'>Stackup, top to bottom: {_e(stackup_summary(snap.stackup))}.</p>")

    # ---------------- tables
    parts.append("<h2 id='loads'>Loads</h2>")
    parts.append(_table(
        ["Load", "Group", "I (A)", "Nominal (V)", "V effective (V)", "Drop / boost (mV)", "%",
         "Worst pin (V)", "Worst pin (mV)", "Worst %", "Tolerance %", "Result"],
        [[_e(l.name), _e(l.group), _num(l.current), _num(l.nominal, ".5g"),
          _num(l.v_effective, ".6f"), fmt_drop(l.drop, unit=""), _num(abs(l.drop_pct), ".3f"),
          _num(l.v_worst, ".6f"), fmt_drop(l.drop_worst, unit=""),
          _num(abs(l.drop_worst_pct), ".3f"),
          "" if l.tolerance_pct is None else f"±{l.tolerance_pct:g}",
          _verdict(l.passed, l.status)]
         for l in tree.loads]))
    parts.append("<p class='note'>Drop: below the VRM set point. Boost: above it, e.g. near a "
                 "remote-sensed VRM's output pins. The worst pin is the one furthest from the "
                 "set point either way, and is judged against ± the tolerance.</p>")
    pg = [[_e(l.name), _e(n), _num(a), _num(vm, ".6f"), _num(vmin, ".6f"), _num(vmax, ".6f")]
          for l in tree.loads for n, a, vm, vmin, vmax in l.pin_groups]
    if pg:
        parts.append("<h3>Load pin groups</h3>" +
                     _table(["Load", "Pin group", "I (A)", "V mean", "V lowest pin",
                             "V highest pin"], pg))
    parts.append("<h2 id='vrms'>VRMs</h2>")
    parts.append(_table(
        ["VRM", "Type", "Group", "V set", "V out", "V sense", "V gnd sense", "I out (A)",
         "V in", "I in (A)", "P out (W)", "P in (W)", "Loss (W)", "I limit (A)", "Result"],
        [[_e(v.name), _e(v.kind), _e(v.group), _num(v.v_set, ".5g"), _num(v.v_out, ".5f"),
          _num(v.v_sense, ".5f"), _num(v.v_ground_sense, ".5f"), _num(v.i_out),
          _num(v.v_in, ".5f"), _num(v.i_in), _num(v.p_out), _num(v.p_in), _num(v.p_loss),
          _num(v.current_limit_a, "g"), _verdict(v.passed)]
         for v in tree.vrms]))
    phases = [[_e(v.name), str(p.index), _num(p.v_out, ".5f"), _num(p.i_out), _num(p.v_in, ".5f"),
               _num(p.i_in), _num(p.p_loss)] for v in tree.vrms for p in v.phases]
    if phases:
        parts.append("<h3>VRM phases</h3>" +
                     _table(["VRM", "Phase", "V out", "I out (A)", "V in", "I in (A)",
                             "Loss (W)"], phases))
    links = [[_e(sr.name), _e(g.name), _num(sr.resistance * 1e3, "g"), _num(sr.current),
              _num(sr.power * 1e3, ".3f")]
             for g in tree.groups.values() for sr in g.result.fields.series]
    if links:
        parts.append("<h3>DC connections</h3>" +
                     _table(["Part", "Group", "R (mΩ)", "I (A)", "P (mW)"], links))
    pins = pin_rows(tree, snap)
    parts.append("<h2 id='pins'>Pins</h2>")
    if pins:
        for name in tree.groups:
            rows = [r for r in pins if r.group == name]
            if not rows:
                continue
            parts.append(
                f"<details><summary><b>{_e(name)}</b>: {len(rows)} pin(s)</summary>" +
                _table(["Terminal", "Role", "Pin", "x (mm)", "y (mm)", "V", "Drop / boost (mV)",
                        "%", "I (mA)", "I is"],
                       [[_e(r.terminal), _e(r.role),
                         _e(f"{r.part}.{r.pin}" if r.part else r.pin), _num(r.x, ".3f"),
                         _num(r.y, ".3f"), _num(r.v, ".6f"), fmt_drop(r.deviation, unit=""),
                         _num(abs(100.0 * r.deviation / r.nominal), ".3f") if r.nominal else "",
                         _num(None if r.current is None else r.current * 1e3, ".4g"),
                         _e(r.current_is)] for r in rows]) +
                "</details>")
        parts.append("<p class='note'>Every pin each group was solved with: VRM outputs, "
                     "phases, inputs and sense pins, load supply and ground pins and the pins "
                     "of DC connections. I is the current drawn from the copper at the pin; "
                     "negative is delivered into it (a VRM output, or a DC connection's far "
                     "side). VRM output pin currents are solved; a load's current is shared "
                     "equally by its pins, as the solver applies it, and a DC connection's by "
                     "the pins on each side. Drop and boost are from the VRM set point (0 V "
                     "for a ground).</p>")
    else:
        parts.append("<p class='note'>These results were saved before KiPI kept each pin "
                     "(0.7.2); solve again to list them.</p>")
    losses = loss_rows(tree, snap)
    layers = [c.name for c in snap.stackup.copper if any(c.name in r.by_layer for r in losses)]
    parts.append("<h2 id='losses'>Losses</h2>")
    parts.append(_table(["Group", *[f"{n} (mW)" for n in layers], "Vias (mW)",
                         "DC parts (mW)", "Total (mW)"],
                        [[_e(r.group), *[_num(r.by_layer.get(n, 0) * 1e3, ".3f") for n in layers],
                          _num(r.vias * 1e3, ".3f"), _num(r.links * 1e3, ".3f"),
                          _num(r.total * 1e3, ".3f")] for r in losses]))
    vr = via_rows(tree, snap, setup.via_current_limit_a)[:25]
    parts.append("<h2 id='vias'>Highest via currents</h2>")
    parts.append(_table(["Group", "Via / pad", "x (mm)", "y (mm)", "Drill (mm)", "I (mA)",
                         "P (mW)", "Result"],
                        [[_e(r.group), _e(r.label), _num(r.x, ".3f"), _num(r.y, ".3f"),
                          _num(r.drill_mm, ".3f"), _num(r.current * 1e3, ".2f"),
                          _num(r.power * 1e3, ".4f"), _verdict(r.passed)] for r in vr]))
    if setup.via_current_limit_a:
        parts.append(f"<p class='note'>Via current limit {setup.via_current_limit_a * 1e3:g} mA.</p>")

    # ---------------- thermal
    if th is not None:
        info("Report: thermal map")
        x, y, tmax = th.hottest()
        parts.append("<h2 id='thermal'>Electro-thermal</h2>")
        parts.append(f"<p>{th.heat_w:.3g} W into the board; peak <b>{tmax:.1f} °C</b> at "
                     f"({x:.1f}, {y:.1f}) mm. Ambient {th.settings.ambient_c:g} °C, airflow "
                     f"{th.settings.airflow_m_s:g} m/s, {len(th.history)} iteration(s).</p>")
        temps = layer_temps(tree, snap, setup.thermal)
        if temps:
            verdict = thermal_passed(temps)
            parts.append("<h3>Copper temperature by layer</h3>" + _table(
                ["Layer", "Peak (°C)", "Rise (°C)", "x (mm)", "y (mm)", "Group", "Result"],
                [[_e(r.layer), _num(r.t_max, ".1f"), _num(r.rise, ".1f"), _num(r.x, ".1f"),
                  _num(r.y, ".1f"), _e(r.group), _verdict(r.passed)] for r in temps]))
            lim = limits_text(setup.thermal)
            parts.append("<p class='note'>The hottest solved copper on each layer. "
                         + (f"Limits: {lim}; overall {_verdict(verdict)}." if lim
                            else "No temperature limits were set.") + "</p>")
        parts.append(f"<div class='maps'><figure><img src='{_thermal_map(th, outline)}'>"
                     "</figure></div>")
        if th.dielectrics:
            parts.append("<h3>Laminate (in-plane thermal conductivity)</h3>" + _table(
                ["Stackup material", "Thickness (mm)", "Taken as", "k (W/m·K)"],
                [[_e(m or "(unnamed)"), _num(t, ".3f"),
                  _e(i.matched) + (" ⚠" if i.assumed else ""), _num(i.k_w_mk, "g")]
                 for m, t, i in th.dielectrics]))
        parts.append(_table(["Iteration", "Heat (W)", "Peak (°C)", "Max change (°C)"],
                            [[str(h["iteration"]), _num(h["heat_w"]), _num(h["t_max"], ".2f"),
                              _num(h["dt_max"], ".3f")] for h in th.history]))

    # ---------------- groups
    for gi, (name, g) in enumerate(tree.groups.items()):
        info(f"Report: {name}")
        scene = scenes[name]
        f = g.result.fields
        parts.append(f"<h2 id='g{gi}'>{_e(name)}</h2>")
        role = (f"driven by {_e(g.driver)}" if g.kind == "rail"
                else f"ground, referenced at {_e(g.driver)}")
        parts.append(f"<p>{role}; nets: {_e(', '.join(g.nets))}.</p>")
        parts.append(_plotly_3d(scene, snap, f"plot{gi}"))
        parts.append("<p class='note'>Drag to rotate, scroll to zoom, double-click to reset; "
                     "hover for values. The 3D mesh is simplified for the report, and its color "
                     "scale spans the 1st–99th percentile of voltages (the maps below are "
                     "scaled per layer).</p>")
        rows = []
        tri_layer = g.result.mesh.tri_layer
        for lid, lr in scene.layers.items():
            v = lr.node_v[np.isfinite(lr.node_v)]
            sel = tri_layer == lid
            area = float(f.tri_area[sel].sum())
            j = lr.tri_j[np.isfinite(lr.tri_j)]
            rows.append([_e(names.get(lid, lid)), _num(area, ".1f"),
                         _num(v.min() if len(v) else None, ".6f"),
                         _num(v.max() if len(v) else None, ".6f"),
                         _num(max(scene.v_ref - v.min(), 0) * 1e3 if len(v) else None, ".3f"),
                         _num(max(v.max() - scene.v_ref, 0) * 1e3 if len(v) else None, ".3f"),
                         _num(np.percentile(j, 99.5) if len(j) else None, ".3g"),
                         _num(j.max() if len(j) else None, ".3g"),
                         _num(float(f.tri_power[sel].sum()) * 1e3, ".3f")])
        below, above = (("Max drop (mV)", "Max boost (mV)") if g.kind == "rail"
                        else ("Max below 0 V (mV)", "Max above 0 V (mV)"))
        parts.append(_table(["Layer", "Copper (mm²)", "V min", "V max", below, above,
                             "|J| p99.5 (A/mm²)", "|J| max", "Loss (mW)"], rows))
        if g.kind == "rail":
            parts.append(f"<p class='note'>Drop and boost are measured from the "
                         f"{scene.v_ref:g} V set point.</p>")
        maps = []
        vmin, vmax = scene.ranges["voltage"]
        for lid, lr in scene.layers.items():
            lname = names.get(lid, str(lid))
            maps.append(_layer_map(lr, lr.node_v, False, f"{lname}: voltage", "turbo", "V",
                                   outline=outline))
            maps.append(_layer_map(lr, lr.tri_j, True, f"{lname}: current density", "inferno",
                                   "A/mm²", clip_pct=99.5, outline=outline))
        parts.append("<div class='maps'>" + "".join(
            f"<figure><img src='{m}'></figure>" for m in maps if m) + "</div>")
        parts.append("<p class='note'>Current-density maps clip at the 99.5th percentile; pad "
                     "corners are singular and would otherwise wash out the map.</p>")

    if tree.warnings:
        parts.append("<h2>Notes</h2><ul>" + "".join(f"<li>{_e(w)}</li>" for w in tree.warnings)
                     + "</ul>")

    js = plotly.offline.get_plotlyjs()
    doc = (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
           f"<title>KiPI report — {_e(snap.board_name)}</title><style>{CSS}</style>"
           f"<script type='text/javascript'>{js}</script></head><body>"
           + "".join(parts) + "</body></html>")
    path.write_text(doc, encoding="utf-8")
    info(f"Report written: {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return path
