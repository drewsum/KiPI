"""Self-contained HTML report for an AC impedance solve of one rail.

One ``.html`` file with:
* a summary and the probe table (worst impedance against each target, PASS / FAIL),
* an interactive impedance plot (Plotly: zoom, pan, hover, click a legend entry to hide a
  curve): |Z| at every probe, the targets, and each capacitor model on its own,
* a board map of the rail's copper with the probes and the capacitors in the model,
* the capacitor models, series links, sweep and mesh settings, and solver statistics.

Plotly's JavaScript is embedded once, so the report works offline and can be shared as a
single file. It uses the same layout and styling as the DC report.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from kipi import __version__
from kipi.ac.model import caps_dict
from kipi.ac.solver.fem import dielectric_summary
from kipi.core.model.board import BoardSnapshot
from kipi.core.naming import timestamp
from kipi.dc.report.html import CSS, SORT_JS, _e, _table, _verdict

COLORS = ("#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#17becf", "#bcbd22",
          "#8c564b", "#e377c2", "#7f7f7f")
COPPER_SIMPLIFY_MM = 0.05            # outline tolerance for the board map
MAX_COPPER_POINTS = 200_000          # beyond this the map shows the board outline only
MAP_WIDTH_PX = 1100                  # about the page width, for the map's height
MAP_MIN_PX, MAP_MAX_PX = 320, 900     # the map's plot height, before the page sizes it
MAP_EXTRA_PX = 110                   # margins, axis title and legend
MAP_MARGIN_L, MAP_MARGIN_R = 50, 20
# Run once the map is drawn and on every resize: fit its height to its width.
MAP_FIT_JS = """(function () {
  var d = document.getElementById('{plot_id}');
  function fit() {
    var h = Math.round(Math.min(Math.max((d.clientWidth - %(side)d) * %(aspect).6f, %(lo)d),
                                %(hi)d)) + %(extra)d;
    if (Math.abs(h - d.clientHeight) < 2) return;
    d.parentElement.style.height = h + 'px';
    Plotly.relayout(d, {height: h});
  }
  fit();
  window.addEventListener('resize', fit);
})();"""
CSS_EXTRA = ".caps td:not(:last-child) { white-space: nowrap; } .caps td:last-child " \
            "{ text-align: left; }"


def report_name(board_stem: str, rail: str, when: Optional[_dt.datetime] = None) -> str:
    """File name of a rail's AC report: '<board>_<rail>_ac_report_<time stamp>.html', the
    rail made safe for a file name ('+3.3V' stays '+3.3V'; '/' and the like become '_').
    The time stamp keeps each solve's report (#66)."""
    safe = re.sub(r"[^\w.+\-]+", "_", rail).strip("_") or "rail"
    return f"{board_stem}_{safe}_ac_report_{timestamp(when)}.html"


def _ohms(z: Optional[float]) -> str:
    """An impedance with a fitting unit: 0.0123 -> '12.3 mΩ'."""
    if z is None or not math.isfinite(z):
        return ""
    for scale, unit in ((1.0, "Ω"), (1e-3, "mΩ")):
        if abs(z) >= scale * 0.9995:
            return f"{z / scale:.4g} {unit}"
    return f"{z * 1e6:.4g} µΩ"


def _hz(f: Optional[float]) -> str:
    if f is None or not math.isfinite(f):
        return ""
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if f >= scale * 0.9995:
            return f"{f / scale:.4g} {unit}"
    return f"{f:.4g} Hz"


def _si(x: float, unit: str) -> str:
    if not x or not math.isfinite(x):
        return ""
    e = max(-15, min(9, 3 * math.floor(math.log10(abs(x)) / 3)))
    prefix = {-15: "f", -12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}[e]
    return f"{x / 10 ** e:.4g} {prefix}{unit}"


def _target_text(c) -> str:
    above = f" above {_hz(c.above_hz)}" if c.above_hz else ""
    if c.target_points:
        return f"custom ({len(c.target_points)} points){above}"
    if c.target_ohm:
        span = (f" from {_hz(c.above_hz)} to {_hz(c.target_hz)}" if c.above_hz and c.target_hz
                else above or (f" to {_hz(c.target_hz)}" if c.target_hz else ""))
        return f"{_ohms(c.target_ohm)}{span}"
    return ""


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def _impedance_plot(result, models: List, div_id: str) -> str:
    """|Z| (log-log) against frequency at every probe, with the targets and, hidden until
    clicked in the legend, each capacitor model's own impedance. Hover gives R and X."""
    import plotly.graph_objects as go
    import plotly.io as pio

    f = result.freqs
    fig = go.Figure()
    hover = ("%{customdata[0]}<br>f %{customdata[1]}<br>|Z| %{customdata[2]}"
             "<br>R %{customdata[3]}<br>X %{customdata[4]}<extra></extra>")
    for i, c in enumerate(result.probes):
        color = COLORS[i % len(COLORS)]
        custom = np.array([[c.name, _hz(fk), _ohms(abs(z)), _ohms(z.real), _ohms(z.imag)]
                           for fk, z in zip(f, c.z)], dtype=object)
        fig.add_trace(go.Scatter(x=f, y=c.mag, name=c.name, legendgroup=c.name,
                                 line=dict(color=color, width=2), customdata=custom,
                                 hovertemplate=hover))
        t = c.target_at(f)
        if t is not None:
            fig.add_trace(go.Scatter(x=f, y=t, name=f"{c.name} target", legendgroup=c.name,
                                     line=dict(color=color, width=1.5, dash="dash"),
                                     connectgaps=False,
                                     hovertemplate=(f"{_e(c.name)} target<br>f %{{x:.4s}}Hz"
                                                    "<br>%{y:.4s}Ω<extra></extra>")))
    omega = 2 * np.pi * f
    for k, (m, n) in enumerate(models):
        if not (m.capacitance_f > 0 and m.esl_h > 0 and m.esr_ohm > 0):
            continue
        fig.add_trace(go.Scatter(
            x=f, y=np.abs(m.impedance(omega)), name=f"{m.name} (one part)",
            visible="legendonly", legendgroup=f"cap{k}",
            line=dict(color="#999", width=1, dash="dot"),
            hovertemplate=(f"{_e(m.name)}, one of {n}<br>f %{{x:.4s}}Hz<br>%{{y:.4s}}Ω"
                           "<extra></extra>")))
    bw = getattr(result, "bandwidth_hz", None)
    if bw and f[0] < bw < f[-1]:
        # Below the converter's control loop bandwidth the loop holds the rail: shade it.
        fig.add_vrect(x0=f[0], x1=bw, fillcolor="#888", opacity=0.08, line_width=0)
        fig.add_vline(x=bw, line=dict(color="#888", width=1, dash="dot"))
        fig.add_annotation(x=math.log10(bw), y=1, xref="x", yref="paper", xanchor="right",
                           yanchor="top", showarrow=False, font=dict(size=11, color="#666"),
                           text=f"VRM bandwidth {_hz(bw)} ")
    fig.update_xaxes(type="log", exponentformat="SI", ticksuffix="Hz", showgrid=True,
                     gridcolor="#e6e6e6", title_text="Frequency")
    fig.update_yaxes(type="log", exponentformat="SI", ticksuffix="Ω", title_text="|Z|",
                     gridcolor="#e6e6e6")
    fig.update_layout(height=560, margin=dict(l=60, r=20, t=20, b=50),
                      plot_bgcolor="white", hovermode="closest",
                      legend=dict(orientation="h", y=-0.12))
    return pio.to_html(fig, include_plotlyjs=False, full_html=False, div_id=div_id,
                       config={"displaylogo": False})


def _rings(geom) -> List[np.ndarray]:
    polys = getattr(geom, "geoms", [geom])
    out = []
    for p in polys:
        if p.is_empty or not hasattr(p, "exterior"):
            continue
        out.append(np.asarray(p.exterior.coords))
        out += [np.asarray(r.coords) for r in p.interiors]
    return out


def _lines(rings) -> tuple:
    xs: List = []
    ys: List = []
    for r in rings:
        xs += list(r[:, 0]) + [None]
        ys += list(r[:, 1]) + [None]
    return xs, ys


def _rail_copper(snap: BoardSnapshot, nets: List[str]) -> List[np.ndarray]:
    """Outline rings of the rail's copper, every layer merged (a top-down silhouette)."""
    import shapely
    from kipi.core.geometry.copper import net_layer_copper
    parts = [net_layer_copper(snap, n, cu.id, cut_drills=False)
             for n in nets for cu in snap.stackup.copper]
    parts = [g for g in parts if not g.is_empty]
    if not parts:
        return []
    geom = shapely.union_all(parts).simplify(COPPER_SIMPLIFY_MM)
    rings = _rings(geom)
    return rings if sum(len(r) for r in rings) <= MAX_COPPER_POINTS else []


def _board_map(result, setup, snap: BoardSnapshot, div_id: str) -> str:
    """Top-down map (KiCad's y down): the board outline, the rail's copper, the probes and
    the capacitors in the model (by model), and those left out for want of one."""
    import plotly.graph_objects as go
    import plotly.io as pio

    fp = {f.reference: f for f in snap.footprints}
    traces = []
    if snap.outline:
        xs, ys = _lines([np.asarray(p.outline + p.outline[:1]) for p in snap.outline
                         if p.outline])
        traces.append(go.Scatter(x=xs, y=ys, mode="lines", name="board",
                                 line=dict(color="#555", width=1.2), hoverinfo="skip"))
    try:
        rings = _rail_copper(snap, result.rail_nets)
    except Exception:                    # geometry trouble: the map works without it
        rings = []
    if rings:
        xs, ys = _lines(rings)
        traces.append(go.Scatter(x=xs, y=ys, mode="lines",
                                 name=f"rail copper ({', '.join(result.rail_nets)})",
                                 line=dict(color="#d9a066", width=1), hoverinfo="skip"))
    model_of = caps_dict(setup.caps)
    by_model: Dict[str, List[str]] = {}
    for ref in result.caps:
        m = model_of.get(ref)
        by_model.setdefault(m.name if m else "?", []).append(ref)
    for k, (name, refs) in enumerate(sorted(by_model.items())):
        refs = [r for r in refs if r in fp]
        if not refs:
            continue
        m = next((mm for mm in setup.caps if mm.name == name), None)
        desc = (f"{_si(m.capacitance_f, 'F')}, {_si(m.esl_h, 'H')}, {_si(m.esr_ohm, 'Ω')}"
                if m else "")
        traces.append(go.Scatter(
            x=[fp[r].position[0] for r in refs], y=[fp[r].position[1] for r in refs],
            mode="markers", name=f"{name} ({len(refs)})",
            marker=dict(size=8, color=COLORS[k % len(COLORS)], line=dict(width=0.5,
                                                                         color="#333")),
            text=[f"{r} · {fp[r].value} · {fp[r].side}<br>model {name}: {desc}" for r in refs],
            hovertemplate="%{text}<extra></extra>"))
    missing = [r for r in result.missing_caps if r in fp]
    if missing:
        traces.append(go.Scatter(
            x=[fp[r].position[0] for r in missing], y=[fp[r].position[1] for r in missing],
            mode="markers", name=f"no model, left out ({len(missing)})",
            marker=dict(size=8, symbol="x", color="#999"),
            text=[f"{r} · {fp[r].value}: no model" for r in missing],
            hovertemplate="%{text}<extra></extra>"))
    names = {c.name for c in result.probes}
    probes = [p for p in setup.probes if p.name in names and p.rail.ref in fp]
    if probes:
        traces.append(go.Scatter(
            x=[fp[p.rail.ref].position[0] for p in probes],
            y=[fp[p.rail.ref].position[1] for p in probes], mode="markers+text",
            name="probes", text=[p.name for p in probes], textposition="top center",
            marker=dict(size=16, symbol="star", color="#d62728",
                        line=dict(width=1, color="#600")),
            hovertemplate="probe %{text}<extra></extra>"))
    fig = go.Figure(traces)
    # Fit the axes to the board and size the plot to its proportions: with the 1:1 aspect
    # lock alone, Plotly widens the x range and leaves the board small in a tall box.
    xy = [np.asarray(p.outline) for p in snap.outline if p.outline] if snap.outline else []
    xy += [np.array([f.position for f in snap.footprints])] if snap.footprints else []
    post_script = None
    if xy:
        pts = np.vstack(xy)
        (x0, y0), (x1, y1) = pts.min(axis=0), pts.max(axis=0)
        pad = 0.03 * max(x1 - x0, y1 - y0, 1.0)
        x0, x1, y0, y1 = x0 - pad, x1 + pad, y0 - pad, y1 + pad
        aspect = (y1 - y0) / (x1 - x0)
        height = int(np.clip(MAP_WIDTH_PX * aspect, MAP_MIN_PX, MAP_MAX_PX)) + MAP_EXTRA_PX
        fig.update_xaxes(range=[x0, x1], constrain="domain")
        fig.update_yaxes(range=[y1, y0], constrain="domain")
        # The page may be narrower than MAP_WIDTH_PX: size the map to the width it gets, so
        # the 1:1 aspect does not leave blank bands above and below the board.
        post_script = MAP_FIT_JS % dict(aspect=aspect, lo=MAP_MIN_PX, hi=MAP_MAX_PX,
                                        extra=MAP_EXTRA_PX, side=MAP_MARGIN_L + MAP_MARGIN_R)
    else:
        height = 560
        fig.update_yaxes(autorange="reversed")
    fig.update_layout(height=height, margin=dict(l=MAP_MARGIN_L, r=MAP_MARGIN_R, t=10, b=40),
                      plot_bgcolor="white",
                      legend=dict(orientation="h", y=-0.06), hovermode="closest")
    fig.update_xaxes(title_text="x (mm)", showgrid=False, zeroline=False)
    fig.update_yaxes(title_text="y (mm)", scaleanchor="x", scaleratio=1, showgrid=False,
                     zeroline=False)
    return pio.to_html(fig, include_plotlyjs=False, full_html=False, div_id=div_id,
                       config={"displaylogo": False}, post_script=post_script)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_ac_report(path, result, snap: BoardSnapshot, setup, progress=None) -> Path:
    """Write the report of ``result`` (an :class:`~kipi.ac.solver.sweep.ACResult`) for the
    rail it solved, with ``setup`` the setup it was solved with."""
    import plotly.offline

    def info(t):
        if progress is not None:
            progress.info(t)

    path = Path(path)
    freqs = result.freqs
    parts: List[str] = []
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    s = setup.ac
    st = result.stats

    # ---------------- summary
    parts.append(f"<h1>KiPI AC impedance report: {_e(result.rail)}</h1>"
                 f"<div class='meta'>{_e(snap.board_name)} · {now} · KiPI {__version__} · "
                 f"solved in {result.solve_s:.1f} s</div>")
    parts.append("<nav><a href='#probes'>Probes</a> <a href='#impedance'>Impedance</a> "
                 "<a href='#map'>Board map</a> <a href='#caps'>Capacitors</a> "
                 "<a href='#model'>Model</a>"
                 + (" <a href='#notes'>Notes</a>" if result.warnings or result.missing_caps
                    else "") + "</nav>")
    parts.append(f"<p>Rail <b>{_e(result.rail)}</b>: {_e(', '.join(result.rail_nets))} against "
                 f"{_e(result.ground)}, from {_hz(freqs[0])} to {_hz(freqs[-1])}. "
                 f"{len(result.caps)} capacitor(s) with models, {len(result.links)} series "
                 f"link(s); plane capacitance {result.plane_capacitance_f * 1e9:.3g} nF.</p>")
    verdicts = [c.passed(freqs) for c in result.probes]
    judged = [v for v in verdicts if v is not None]
    if judged:
        parts.append(f"<p>Overall: {_verdict(all(judged))} "
                     f"({sum(judged)} of {len(judged)} probe(s) with a target pass).</p>")

    # ---------------- probes
    by_name = {p.name: p for p in setup.probes}
    rows = []
    for c, ok in zip(result.probes, verdicts):
        zmax, fmax = c.worst(freqs)
        p = by_name.get(c.name)
        pins = _e(f"{p.rail.label()} / {p.ground.label()}") if p is not None else ""
        t = c.target_at(np.array([fmax])) if c.has_target else None
        margin = ""
        if t is not None and np.isfinite(t[0]) and t[0] > 0:
            margin = f"{20 * math.log10(t[0] / zmax):+.2f} dB"
        rows.append([_e(c.name), pins, _ohms(float(np.min(c.mag))), _ohms(zmax), _hz(fmax),
                     _e(_target_text(c)), margin, _verdict(ok)])
    parts.append("<h2 id='probes'>Probes</h2>")
    parts.append(_table(["Probe", "Pins (rail / ground)", "Min |Z|", "Worst |Z|", "at",
                         "Target", "Margin", "Result"], rows))
    parts.append("<p class='note'>Worst |Z|: where the impedance comes closest to (or furthest "
                 "over) the probe's target, up to where the target applies; the highest "
                 "impedance in the sweep without one. Margin: target over |Z| there (positive "
                 "passes).</p>")
    if getattr(result, "bandwidth_hz", None):
        parts.append(f"<p class='note'>The VRM is out of circuit, so this is the open-loop PDN "
                     f"impedance. Below the converter's control loop bandwidth "
                     f"({_hz(result.bandwidth_hz)}) the loop holds the rail, so targets are "
                     "drawn and judged only above it.</p>")

    # ---------------- impedance plot
    info("Report: impedance plot")
    model_of = caps_dict(setup.caps)
    counts: Dict[str, int] = {}
    for ref in result.caps:
        if ref in model_of:
            counts[model_of[ref].name] = counts.get(model_of[ref].name, 0) + 1
    models = [(m, counts[m.name]) for m in setup.caps if m.name in counts]
    parts.append("<h2 id='impedance'>Impedance</h2>")
    parts.append(_impedance_plot(result, models, "zplot"))
    parts.append("<p class='note'>Drag to zoom, double-click to reset, hover for values. "
                 "Click a legend entry to hide or show a curve: the grey dotted ones are each "
                 "capacitor model on its own (one part, without its mounting), for "
                 "comparing resonances.</p>")

    # ---------------- board map
    info("Report: board map")
    parts.append("<h2 id='map'>Board map</h2>")
    parts.append(_board_map(result, setup, snap, "mapplot"))   # not "map": the heading's id
    parts.append("<p class='note'>Seen from the top, with the rail's copper on every layer "
                 "merged. Hover a marker for the part; click a legend entry to hide a "
                 "group.</p>")

    # ---------------- capacitors
    parts.append("<h2 id='caps'>Capacitor models</h2>")
    rows = []
    for m, n in models:
        refs = sorted((r for r in result.caps if model_of.get(r) is m), key=_natural)
        srf = m.srf_hz()
        rows.append([_e(m.name), _si(m.capacitance_f, "F"), _si(m.esl_h, "H"),
                     _si(m.esr_ohm, "Ω"), _hz(srf) if srf else "", str(n),
                     _e(", ".join(refs))])
    parts.append(_table(["Model", "C", "ESL", "ESR", "Self-resonance", "Parts on rail",
                         "References"], rows, "caps"))
    total_c = sum(m.capacitance_f * n for m, n in models)
    parts.append(f"<p class='note'>{_si(total_c, 'F')} of capacitors in the model, plus "
                 f"{result.plane_capacitance_f * 1e9:.3g} nF between the rail's copper and "
                 f"{_e(result.ground)}. ESL is the part's own; the mounting (pads, vias, "
                 "planes) comes from the field solve.</p>")

    # ---------------- model
    parts.append("<h2 id='model'>Model</h2>")
    links = [k for k in setup.links if k.name in set(result.links)]
    if links:
        parts.append("<h3>Series links</h3>" + _table(
            ["Link", "Side A", "Side B", "R", "ESL"],
            [[_e(k.name), _e(f"{k.side_a.label()} · {k.side_a.net}"),
              _e(f"{k.side_b.label()} · {k.side_b.net}"), _ohms(k.resistance),
              _si(k.esl_h, "H") if k.esl_h else ""] for k in links]))
    ff = result.field_freqs
    parts.append("<h3>Sweep and mesh</h3>" + _table(["Setting", "Value"], [
        ["Sweep", f"{_hz(freqs[0])} to {_hz(freqs[-1])}, {len(freqs)} points "
                  f"({s.points_per_decade} per decade)"],
        ["Field solves", f"{len(ff)} ({s.field_points_per_decade or 'every point'} per decade; "
                         "the copper's response is interpolated in between)"],
        ["Mesh", f"max edge {s.max_edge_mm:g} mm, min edge {s.min_edge_mm:g} mm at plated "
                 f"holes, grading {s.grading:g}"],
        ["Dielectric", f"loss tangent {s.loss_tangent:g}; εr top to bottom: "
                       f"{dielectric_summary(snap.stackup, s.default_er)}"],
        ["Copper", f"resistivity {setup.settings.rho_ohm_mm * 1e-3:.4g} Ω·m, barrel plating "
                   f"{setup.settings.plating_mm * 1000:g} µm"],
    ]))
    times = ", ".join(f"{k} {v:.1f} s" for k, v in st.times.items())
    parts.append("<h3>Solver</h3>" + _table(["Quantity", "Value"], [
        ["Mesh", f"{st.n_nodes} nodes, {st.n_tris} triangles"],
        ["System", f"{st.n_unknowns} unknowns, {st.nnz} non-zeros, {st.n_ports} ports"],
        ["Direct solver", _e(st.method)],
        ["Time", f"{result.solve_s:.1f} s ({times})" if times else f"{result.solve_s:.1f} s"],
    ]))

    # ---------------- notes
    notes = list(result.warnings)
    if result.missing_caps:
        notes.insert(0, f"{len(result.missing_caps)} capacitor(s) between the rail and "
                        f"{result.ground} have no model and were left out: "
                        + ", ".join(sorted(result.missing_caps, key=_natural)))
    if notes:
        parts.append("<h2 id='notes'>Notes</h2><ul>"
                     + "".join(f"<li>{_e(w)}</li>" for w in notes) + "</ul>")

    js = plotly.offline.get_plotlyjs()
    doc = (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
           f"<title>KiPI AC report — {_e(result.rail)} — {_e(snap.board_name)}</title>"
           f"<style>{CSS}{CSS_EXTRA}</style><script type='text/javascript'>{js}</script>"
           f"<script type='text/javascript'>{SORT_JS}</script></head><body>"
           + "".join(parts) + "</body></html>")
    path.write_text(doc, encoding="utf-8")
    info(f"Report written: {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return path


def _natural(ref: str):
    """C2 before C10."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref)]
