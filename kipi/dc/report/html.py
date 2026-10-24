"""Self-contained HTML (or PDF) report for a solved power tree.

One ``.html`` file with:
* a summary and the load / VRM / DC connection / pin / loss / via tables,
* per net group: an interactive 3D view (Plotly, rotate and zoom in the browser), a
  per-layer table and top-down voltage and current-density maps for every layer, with a
  tickbox per layer that hides or shows it in the view and the maps (#181),
* the board temperature map and iteration history when an electro-thermal solve ran.

Plotly's JavaScript is embedded once, so the report works offline and can be shared
as a single file. Images are rendered with matplotlib's Agg backend (thread safe).

A ``.pdf`` path gets the same report printed (#119): the tables and maps, with the notes
box as a form field. The 3D views need a browser, so they go in a web page of their own,
attached to the PDF (#133): '<report>_3d.html', listed in the PDF viewer's attachments.
"""

from __future__ import annotations

import base64
import datetime as _dt
import html
import io
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from kipi import __version__
from kipi.core.model.board import BoardSnapshot
from kipi.core.model.stackup import summary as stackup_summary
from kipi.core.naming import timestamp
from kipi.dc.pi import vunits

TRIANGLE_BUDGET = 60_000      # per group's 3D view
VIEW_NOTE = ("Drag to rotate, scroll to zoom, double-click to reset; hover for values. The 3D "
             "mesh is simplified for the report, and its color scale spans the 1st–99th "
             "percentile of voltages (the maps are scaled per layer).")
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


class _Html(str):
    """A table header already written as HTML."""


def _table(headers: List[str], rows: List[List[str]], cls: str = "") -> str:
    head = "".join(f"<th title='Click to sort'>{h if isinstance(h, _Html) else _e(h)}</th>"
                   for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _verdict(passed: Optional[bool], text: str = "") -> str:
    """PASS / FAIL, or ``text`` when the result was not judged or failed for a reason."""
    if passed is None:
        return f'<span class="unjudged">{_e(text)}</span>' if text else ""
    label = _e(text) if text else ("PASS" if passed else "FAIL")
    return f'<span class="pass">{label}</span>' if passed else f'<span class="fail">{label}</span>'


# A PDF report's layer maps (#184): one to a row, two to a page, drawn bigger and sharper
# than the web page's. The box is in the 96 dpi pixels of the PDF's layout, the page's width
# (A4 or Letter less its margins) by a little under half its height.
PDF_MAP_DPI = 250                    # about 210 dpi on the page, once cropped
PDF_MAP_SIZE = (7.0, 5.6)            # inches, about the box's size, so the text keeps its size
PDF_MAP_BOX = (690, 440)


def _png(fig, dpi: int = 110) -> str:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    FigureCanvasAgg(fig)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor="white")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# A voltage in the unit the reader picks (#177): mV or % from the target voltage, or
# volts. The web page has the value in each, one shown at a time; a PDF shows all three.
VUNIT_KEYS = {vunits.MV: "mv", vunits.PCT: "pct", vunits.VOLTS: "v"}


def _units(texts: Dict[str, str], pdf: bool) -> str:
    """A cell's ``texts`` ({unit: text}): spans the unit selector picks between, or in a
    PDF, the three on lines of their own with their units."""
    if pdf:
        return "<br>".join(f"{_e(t)} {_e(u)}" for u, t in texts.items() if t)
    return "".join(f"<span class='vu vu-{VUNIT_KEYS[u]}'>{_e(t)}</span>"
                   for u, t in texts.items())


def _vcell(v: Optional[float], target: Optional[float], pdf: bool) -> str:
    """The voltage ``v`` in each unit, from ``target``."""
    return _units({u: vunits.show(v, target, u) for u, _ in vunits.UNITS}, pdf)


def _vhead(name: str, pdf: bool) -> str:
    """A column header whose unit follows the selector (a PDF's cells give their own)."""
    if pdf:
        return name
    return _Html(f"{_e(name)} (" + "".join(f"<span class='vu vu-{VUNIT_KEYS[u]}'>{_e(u)}</span>"
                                           for u, _ in vunits.UNITS) + ")")


def vunit_selector() -> str:
    """The web page's unit selector, in mV from the target until changed."""
    opts = "".join(f"<option value='{VUNIT_KEYS[u]}'>{_e(label)}</option>"
                   for u, label in vunits.UNITS)
    return (f"<p class='vunit'><label><b>Voltages:</b> <select id='vunit'>{opts}</select>"
            "</label> <span class='note'>From the target voltage, negative is a drop below "
            "it and positive a boost above it.</span></p>")


VUNIT_JS = r"""
(function () {
  var sel = document.getElementById("vunit");
  if (!sel) return;
  sel.addEventListener("change", function () {
    document.documentElement.setAttribute("data-vunit", sel.value);
  });
})();
"""


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def _layer_map(lr, values, cell: bool, title: str, cmap: str, unit: str,
               clip_pct: Optional[float] = None, outline=None,
               v_ref: Optional[float] = None, pdf: bool = False) -> str:
    """Top-down tripcolor of one layer (KiCad y down, drawn the same way as in KiCad).

    A voltage map passes its target voltage ``v_ref``. When its spread is so narrow that the
    colorbar would label ticks against an offset ("1e-5+3.2999", #145), it is redrawn as
    the drop from ``v_ref`` in V, mV or µV, the absolute range moving into the title.
    Maps whose colorbar reads plainly are left as they are. A PDF's map (``pdf``) is drawn
    bigger, at a higher resolution."""
    vals = np.asarray(values, dtype=float)
    finite = vals[np.isfinite(vals)]
    if len(finite) == 0:
        return ""
    size = PDF_MAP_SIZE if pdf else (5.2, 4.2)
    fig, cb = _draw_map(lr, vals, finite, cell, title, cmap, unit, clip_pct, outline, size)
    if v_ref is not None and _has_offset(fig, cb):
        lo, hi = float(finite.min()), float(finite.max())
        rail = v_ref != 0
        shift = (v_ref - vals) if rail else vals
        big = float(np.abs(shift[np.isfinite(shift)]).max())
        scale, prefix = (1.0, "") if big >= 1 else (1e3, "m") if big >= 1e-3 else (1e6, "µ")
        shift = shift * scale
        label = (f"Drop from {v_ref:g} V ({prefix}V)" if rail
                 else f"Offset from 0 V ({prefix}V)")
        fig, cb = _draw_map(lr, shift, shift[np.isfinite(shift)], cell,
                            f"{title}, {lo:.6f} to {hi:.6f} V", cmap, label, clip_pct, outline,
                            size)
        cb.formatter.set_useOffset(False)
    return _png(fig, PDF_MAP_DPI if pdf else 110)


def _has_offset(fig, cb) -> bool:
    """Whether the colorbar's ticks are labelled against an offset or a multiplier."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    FigureCanvasAgg(fig).draw()
    fmt = cb.ax.yaxis.get_major_formatter()
    return bool(getattr(fmt, "get_offset", lambda: "")())


def _draw_map(lr, vals, finite, cell, title, cmap, unit, clip_pct, outline, size=(5.2, 4.2)):
    from matplotlib.figure import Figure
    from matplotlib.tri import Triangulation
    pts, tris = lr.pts2, lr.tris
    fig = Figure(figsize=size)
    ax = fig.add_subplot(111)
    tri = Triangulation(pts[:, 0], pts[:, 1], tris)
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
    return fig, cb


def _thermal_maps(th, snap: BoardSnapshot, outline, pdf: bool = False) -> List[str]:
    """One map per copper layer of the result, on one color scale; a result without
    layers is one board map. A PDF's (``pdf``) are drawn bigger, at a higher resolution."""
    names = {cu.id: cu.name for cu in snap.stackup.copper}
    layers = [lid for lid in th.layers if lid in names] if len(th.layers) == len(th.temps)         else []
    lo, hi = float(np.nanmin(th.temps)), float(np.nanmax(th.temps))
    if not layers:
        return [_thermal_map(th, outline, None, "Board temperature", lo, hi, pdf)]
    return [_thermal_map(th, outline, lid, names[lid], lo, hi, pdf) for lid in layers]


def _thermal_map(th, outline, layer, title, vmin, vmax, pdf: bool = False) -> str:
    from matplotlib.figure import Figure
    fig = Figure(figsize=PDF_MAP_SIZE if pdf else (6.0, 4.8))
    ax = fig.add_subplot(111)
    ext = [th.x[0], th.x[-1], th.y[-1], th.y[0]]
    m = ax.imshow(th.layer_temps(layer), extent=ext, cmap="inferno", origin="upper",
                  interpolation="bilinear", vmin=vmin, vmax=vmax)
    if outline is not None:
        for ring in outline:
            a = np.asarray(ring + ring[:1])
            ax.plot(a[:, 0], a[:, 1], color="#bbb", lw=0.6)
    x, y, t = th.hottest(layer)
    ax.plot([x], [y], marker="+", color="cyan", ms=12)
    ax.set_aspect("equal")
    ax.set_title(f"{title} (peak {t:.1f} °C)", fontsize=10)
    ax.set_xlabel("x (mm)", fontsize=8)
    ax.set_ylabel("y (mm)", fontsize=8)
    ax.tick_params(labelsize=7)
    cb = fig.colorbar(m, ax=ax, shrink=0.85)
    cb.set_label("°C", fontsize=8)
    return _png(fig, PDF_MAP_DPI if pdf else 110)


def _decimate(sheet, keep: float):
    """Thin a flat copper sheet to about ``keep`` of its triangles for the 3D view.

    Only interior vertices are removed (VTK's DecimatePro, topology and boundary kept), so
    every vertex left is an original one: the copper keeps its outline, holes and solved
    voltages. Quadric decimation, used before, is singular on a flat sheet and moved
    vertices off the copper, drawing triangles across holes and off the board (#102).
    Boundary vertices stay, so a sheet with much outline keeps more than ``keep``. On any
    VTK failure the sheet is drawn whole."""
    from vtkmodules.vtkCommonCore import vtkObject
    was = vtkObject.GetGlobalWarningDisplay()
    vtkObject.GlobalWarningDisplayOff()
    try:
        out = sheet.decimate_pro(1.0 - keep, preserve_topology=True,
                                 boundary_vertex_deletion=False)
    except Exception:
        return sheet
    finally:
        vtkObject.SetGlobalWarningDisplay(was)
    return out if out.n_cells and "V" in out.point_data else sheet


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
            sheet = _decimate(sheet, keep)
        tri = sheet.faces.reshape(-1, 4)[:, 1:]
        p = np.asarray(sheet.points)
        traces.append(go.Mesh3d(
            x=p[:, 0], y=p[:, 1], z=p[:, 2], i=tri[:, 0], j=tri[:, 1], k=tri[:, 2],
            intensity=np.asarray(sheet.point_data["V"]), colorscale="Turbo", cmin=vmin,
            cmax=vmax, name=cu.name, showscale=not traces, flatshading=True,
            # Lit evenly, so a color is only its voltage: shading each flat facet lit the
            # thin triangles left by thinning at random, a patchwork of dark ones (#122).
            lighting=dict(ambient=1.0, diffuse=0.0, specular=0.0, roughness=1.0, fresnel=0.0),
            colorbar=dict(title="V", len=0.7),
            hovertemplate=(f"{_e(cu.name)}<br>x %{{x:.2f}} mm<br>y %{{y:.2f}}"
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
.vu { display: none; }
html:not([data-vunit]) .vu-mv, html[data-vunit=mv] .vu-mv, html[data-vunit=pct] .vu-pct,
html[data-vunit=v] .vu-v { display: inline; }
.vunit { position: sticky; top: 0; background: #fafafa; padding: 6px 0; margin: 8px 0;
         z-index: 2; }
.layer-toggles { font-size: 13px; margin: 6px 0; }
.layer-toggles label { margin-right: 12px; white-space: nowrap; cursor: pointer; }
details > summary { cursor: pointer; margin: 6px 0; }
thead th { cursor: pointer; user-select: none; }
thead th[data-sort=asc]::after { content: " \\25B2"; } thead th[data-sort=desc]::after { content: " \\25BC"; }
#user-notes-text { min-height: 4em; background: white; border: 1px solid #ccc; padding: 8px 10px;
                   border-radius: 3px; white-space: pre-wrap; outline: none; }
#user-notes-text:focus { border-color: #4a90d9; }
#user-notes-text.empty::before { content: attr(data-placeholder); color: #999; }
#user-notes-text img { max-width: 100%; }
.notes-bar { margin-top: 6px; font-size: 12px; color: #666; }
.notes-bar button { margin-right: 10px; }
@media print { .notes-bar { display: none; } #user-notes-text { border: none; padding: 0; }
               #user-notes-text.empty::before { content: none; } .notes-empty { display: none; } }
"""

# Notes anyone reading the report can type in (#128). A report is a file opened from disk,
# which a page cannot write to, so the notes are kept in the browser as they are typed
# (localStorage, per file) and "Save a copy with notes" downloads the report with them in
# it, to keep or share. Plotly's drawn plots are emptied in the copy; it draws them again
# when the copy is opened.
NOTES_JS = r"""
(function () {
  var box = document.getElementById("user-notes-text");
  var section = document.getElementById("user-notes-section");
  var status = document.getElementById("user-notes-status");
  var key = "kipi-notes:" + location.pathname;
  function store(f) { try { return f(window.localStorage); } catch (e) { return undefined; } }
  function empty() { return !box.textContent.trim() && !box.querySelector("img"); }
  function mark() {
    box.classList.toggle("empty", empty());
    section.classList.toggle("notes-empty", empty());
  }
  var draft = store(function (s) { return s.getItem(key); });
  if (draft !== null && draft !== undefined && draft !== box.innerHTML) {
    box.innerHTML = draft;
    status.textContent = "Notes restored from this browser.";
  }
  mark();
  box.addEventListener("input", function () {
    mark();
    var ok = store(function (s) { s.setItem(key, box.innerHTML); return true; });
    status.textContent = ok ? "Kept in this browser. Save a copy to keep them in the file."
                            : "This browser cannot keep them: save a copy before closing.";
  });
  document.getElementById("user-notes-save").addEventListener("click", function () {
    var root = document.documentElement.cloneNode(true);
    root.querySelectorAll(".plotly-graph-div").forEach(function (d) { d.innerHTML = ""; });
    root.querySelectorAll("head style:not([data-kipi])").forEach(function (s) { s.remove(); });
    var st = root.querySelector("#user-notes-status");
    if (st) st.textContent = "Notes are kept in this browser as you type.";
    var blob = new Blob(["<!DOCTYPE html>" + root.outerHTML], {type: "text/html"});
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = decodeURIComponent(location.pathname.split("/").pop()) || "kipi_report.html";
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  });
})();
"""


# The layer tickboxes over a group's 3D view (#181): each hides or shows its layer's sheet
# in the view (the vias too) and the layer's maps below it. The colour bar goes with the
# first layer shown.
LAYERS_JS = r"""
document.addEventListener("change", function (e) {
  var bar = e.target.closest ? e.target.closest(".layer-toggles") : null;
  if (!bar) return;
  var id = bar.getAttribute("data-plot"), hidden = {};
  bar.querySelectorAll("input[data-layer]").forEach(function (b) {
    if (!b.checked) hidden[b.getAttribute("data-layer")] = true;
  });
  var plot = document.getElementById(id);
  if (plot && plot.data && window.Plotly) {
    var shown = plot.data.map(function (t) { return !hidden[t.name]; });
    var first = shown.findIndex(function (s, i) {
      return s && plot.data[i].type === "mesh3d"; });
    Plotly.restyle(plot, {visible: shown,
                          showscale: plot.data.map(function (t, i) { return i === first; })});
  }
  document.querySelectorAll("figure[data-plot='" + id + "']").forEach(function (f) {
    f.style.display = hidden[f.getAttribute("data-layer")] ? "none" : "";
  });
});
"""


def layer_toggles(div_id: str, layers: List[str], vias: bool) -> str:
    """A tickbox per copper layer of the 3D view ``div_id`` (and its vias), all ticked."""
    boxes = [(n, n) for n in layers] + ([("vias", "Vias")] if vias else [])
    return (f"<div class='layer-toggles' data-plot='{_e(div_id)}'><b>Layers:</b> "
            + "".join(f"<label><input type='checkbox' checked data-layer='{_e(key)}'> "
                      f"{_e(text)}</label>" for key, text in boxes) + "</div>")


def notes_section(pdf: bool = False) -> str:
    """The editable notes box (#128); in a PDF, a box with a form field to type in."""
    if pdf:
        from kipi.core.pdf import NOTES_MARKER
        return ("<h2 id='user-notes'>User notes</h2>"
                f"<table class='notes'><tr><td>{NOTES_MARKER}</td></tr></table>")
    return ("<section id='user-notes-section'><h2 id='user-notes'>User notes</h2>"
            "<div id='user-notes-text' contenteditable='true' spellcheck='true' "
            "data-placeholder='Click to add notes: what was checked, by whom, what to change…'>"
            "</div>"
            "<div class='notes-bar'><button type='button' id='user-notes-save'>Save a copy "
            "with notes</button><span id='user-notes-status'>Notes are kept in this browser "
            "as you type.</span></div>"
            f"<script type='text/javascript'>{NOTES_JS}</script></section>")


# Click a column header to sort the table by it; click again to reverse (#85). The keys
# match the plugin's tables (kipi.gui.sortable): numbers with SI prefixes, natural text,
# blanks last either way.
SORT_JS = r"""
(function () {
  var NUM = /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?/;
  var PRE = /^\s?([pnuµμmkKMG])(?=$|[FHΩRVAWsSz]|\d|\s)(\d*)/;
  var SCALE = {p: 1e-12, n: 1e-9, u: 1e-6, "µ": 1e-6, "μ": 1e-6, m: 1e-3, k: 1e3, K: 1e3,
               M: 1e6, G: 1e9};
  function key(text) {
    var t = text.replace(/±/g, "").trim();
    if (!t) return [2, 0];
    var m = NUM.exec(t);
    if (!m) return [1, t];
    var x = parseFloat(m[0]), p = PRE.exec(t.slice(m[0].length));
    if (p && m[2] === undefined) {
      if (p[2] && m[0].indexOf(".") < 0) x = parseFloat(m[0] + "." + p[2]);
      x *= SCALE[p[1]];
    }
    return [0, x];
  }
  function compare(a, b) {
    if (a[0] !== b[0]) return a[0] - b[0];
    if (a[0] === 0) return a[1] - b[1];
    return a[1].localeCompare(b[1], undefined, {numeric: true, sensitivity: "base"});
  }
  document.addEventListener("click", function (e) {
    var th = e.target.closest ? e.target.closest("thead th") : null;
    if (!th) return;
    var table = th.closest("table"), body = table.tBodies[0];
    if (!body) return;
    var col = th.cellIndex, desc = th.getAttribute("data-sort") === "asc";
    table.querySelectorAll("thead th").forEach(function (h) { h.removeAttribute("data-sort"); });
    th.setAttribute("data-sort", desc ? "desc" : "asc");
    var unit = document.documentElement.getAttribute("data-vunit") || "mv";
    var rows = Array.prototype.map.call(body.rows, function (r, i) {
      var c = r.cells[col], shown = c && c.querySelector(".vu-" + unit);
      return {row: r, i: i, k: key(shown ? shown.textContent : c ? c.textContent : "")};
    });
    rows.sort(function (a, b) {
      var blank = (a.k[0] === 2) - (b.k[0] === 2);
      if (blank) return blank;                      // blanks last either way
      return (desc ? -1 : 1) * compare(a.k, b.k) || a.i - b.i;
    });
    rows.forEach(function (r) { body.appendChild(r.row); });
  });
})();
"""


def report_name(board_stem: str, when: Optional[_dt.datetime] = None, fmt: str = "html") -> str:
    """File name of the DC report: '<board>_kipi_report_<time stamp>.html' (or .pdf), so
    each solve keeps its own report (#66)."""
    return f"{board_stem}_kipi_report_{timestamp(when)}.{fmt}"


def png_size(data_url: str) -> Optional[Tuple[int, int]]:
    """(width, height) in pixels of a PNG ``data:`` URL, from its header; None if it is
    not one."""
    head = data_url.partition("base64,")[2][:44]
    try:
        raw = base64.b64decode(head + "=" * (-len(head) % 4))
    except ValueError:
        return None
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


def _fit_width(image: str, box=PDF_MAP_BOX) -> int:
    """The width to show ``image`` at on a PDF page so it fills ``box`` (w, h) without
    overflowing it."""
    size = png_size(image)
    if not size or not size[1]:
        return box[0]
    return int(min(box[0], box[1] * size[0] / size[1]))


def maps_html(images: List[str], pdf: bool, tags: Optional[List[tuple]] = None,
              large: bool = False) -> str:
    """Figures side by side: wrapped by the browser, two to a row on a PDF page, or with
    ``large``, one to a row and two to a page (#184). ``tags`` give each image's (3D view,
    layer), for the layer tickboxes (#181)."""
    tagged = [(m, t) for m, t in zip(images, tags or [None] * len(images)) if m]
    images = [m for m, _t in tagged]
    if not pdf:
        return "<div class='maps'>" + "".join(
            f"<figure data-plot='{_e(t[0])}' data-layer='{_e(t[1])}'><img src='{m}'></figure>"
            if t else f"<figure><img src='{m}'></figure>" for m, t in tagged) + "</div>"
    if large:
        # From the top of a page, so each page holds a pair: a layer's voltage and current
        # density maps.
        return "<table class='maps' style='page-break-before: always'>" + "".join(
            f"<tr><td><img src='{m}' width='{_fit_width(m)}'></td></tr>" for m in images
        ) + "</table>"
    width = 330 if len(images) > 1 else 440
    rows = [images[i:i + 2] for i in range(0, len(images), 2)]
    return "<table class='maps'>" + "".join(
        "<tr>" + "".join(f"<td><img src='{m}' width='{width}'></td>" for m in r) + "</tr>"
        for r in rows) + "</table>"


def page(title: str, body: str, css: str, script: str = "") -> str:
    """A web page that works offline: Plotly's JavaScript in it."""
    import plotly.offline
    js = plotly.offline.get_plotlyjs()
    extra = f"<script type='text/javascript'>{script}</script>" if script else ""
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{_e(title)}</title><style data-kipi>{css}</style>"
            f"<script type='text/javascript'>{js}</script>{extra}</head><body>"
            + body + "</body></html>")


def finish(path: Path, title: str, parts: List[str], css: str, pdf: bool,
           attachments=()) -> Path:
    """Write the report's body as a page that works offline, or print it to a PDF with
    ``attachments`` ((file name, content, description) each) in it."""
    body = "".join(parts)
    if pdf:
        from kipi.core.pdf import write_pdf
        return write_pdf(f"<html><head><meta charset='utf-8'><title>{_e(title)}</title>"
                         f"</head><body>{body}</body></html>", path, title,
                         attachments=attachments)
    path.write_text(page(title, body, css, SORT_JS + LAYERS_JS + VUNIT_JS), encoding="utf-8")
    return path


def views_name(path: Path) -> str:
    """The file name of a PDF report's attached 3D views (#133)."""
    return f"{Path(path).stem}_3d.html"


def _views_link(path: Path) -> str:
    """A PDF report's link to its attached 3D views; nothing may follow it on its line,
    where the paperclip goes."""
    from kipi.core.pdf import file_link
    name = views_name(path)
    return f"<a href='{_e(file_link(name))}'>{_e(name)}</a>"


def views_page(title: str, meta: str, views: List[tuple]) -> bytes:
    """The 3D views of a PDF report, as a page of their own: ``views`` are (group name,
    Plotly HTML) pairs."""
    body = [f"<h1>{_e(title)}</h1><div class='meta'>{meta}</div>",
            f"<p class='note'>{_e(VIEW_NOTE)}</p>"]
    for name, view in views:
        body.append(f"<h2>{_e(name)}</h2>{view}")
    return page(title, "".join(body), CSS, LAYERS_JS).encode("utf-8")


def write_report(path, tree, scenes: Dict[str, object], snap: BoardSnapshot, setup,
                 progress=None) -> Path:
    """Write the report to ``path``: a web page, or a PDF when it ends in .pdf (#119)."""
    from kipi.dc.pi.report import (
        layer_temps, limits_text, loss_rows, pin_rows, thermal_passed, via_rows,
    )

    def info(t):
        if progress is not None:
            progress.info(t)

    path = Path(path)
    pdf = path.suffix.lower() == ".pdf"
    views: List[tuple] = []                     # a PDF's 3D views, attached (#133)
    outline = [p.outline for p in snap.outline] if snap.outline else None
    names = {c.id: c.name for c in snap.stackup.copper}
    parts: List[str] = []
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    th = getattr(tree, "thermal", None)

    # ---------------- summary
    meta = (f"{_e(snap.board_name)} · {now} · KiPI {__version__} · solved in "
            f"{tree.solve_s:.1f} s")
    parts.append(f"<h1>KiPI power-integrity report</h1><div class='meta'>{meta}</div>")
    nav = ["<a href='#loads'>Loads</a>", "<a href='#vrms'>VRMs</a>",
           "<a href='#pins'>Pins</a>", "<a href='#losses'>Losses</a>",
           "<a href='#vias'>Vias</a>"]
    if th is not None:
        nav.append("<a href='#thermal'>Thermal</a>")
    nav += [f"<a href='#g{i}'>{_e(n)}</a>" for i, n in enumerate(tree.groups)]
    nav.append("<a href='#user-notes'>User notes</a>")
    if not pdf:
        parts.append("<nav>" + " ".join(nav) + "</nav>")
        parts.append(vunit_selector())
    s = setup.settings
    parts.append(f"<p class='note'>Mesh: max edge {s.max_edge_mm:g} mm, via grading "
                 f"{s.grading:g}, copper resistivity {s.rho_ohm_mm * 1e-3:.4g} Ω·m at 20 °C "
                 f"(TCR {s.tcr_per_c:g}/°C)"
                 + ("; layer overrides: " + ", ".join(
                     f"{_e(n)} {_e(s.conductor_by_layer.get(n, ''))} {r * 1e-3:.4g} Ω·m "
                     f"(TCR {s.tcr_for(n):g}/°C)".replace("  ", " ")
                     for n, r in sorted(s.rho_by_layer.items()))
                    if s.rho_by_layer else "")
                 + ". Via plating, first matching rule (drill = hole in the laminate, plated "
                 f"inward): {_e('; '.join(r.label() for r in s.via_rules))}.</p>")
    parts.append(f"<p class='note'>Stackup, top to bottom: {_e(stackup_summary(snap.stackup))}.</p>")
    if pdf and tree.groups:
        parts.append(f"<p><b>3D Results View:</b> {_views_link(path)}</p>")

    # ---------------- tables
    parts.append("<h2 id='loads'>Loads</h2>")

    def load_row(l):
        cells = {u: vunits.load_cells(l, u) for u, _ in vunits.UNITS}
        return [_e(l.name), _e(l.group), _num(l.current), _num(l.nominal, ".5g"),
                *[_units({u: cells[u][k] for u in cells}, pdf) for k in range(5)],
                _verdict(l.passed, l.status)]

    parts.append(_table(["Load", "Group", "I (A)", "Target (V)",
                         *[_vhead(n, pdf) for n in vunits.LOAD_COLUMNS], "Result"],
                        [load_row(l) for l in tree.loads]))
    parts.append("<p class='note'>Each load's mean voltage and its lowest and highest supply "
                 "pins (less the ground pins), "
                 + ("each in mV and % from the target voltage and in volts. " if pdf else "")
                 + "From the target, negative is a drop below it and positive a boost above "
                 "it, e.g. near a remote-sensed VRM's output pins. The margin is how far the "
                 "pins are inside the tolerance, negative when outside. A load passes when "
                 "every pin is within its tolerance.</p>")
    pg = [[_e(l.name), _e(n), _num(a), _vcell(vm, l.nominal, pdf), _vcell(vmin, l.nominal, pdf),
           _vcell(vmax, l.nominal, pdf)]
          for l in tree.loads for n, a, vm, vmin, vmax in l.pin_groups]
    if pg:
        parts.append("<h3>Load pin groups</h3>" +
                     _table(["Load", "Pin group", "I (A)", _vhead("Mean", pdf),
                             _vhead("Lowest pin", pdf), _vhead("Highest pin", pdf)], pg))
    parts.append("<h2 id='vrms'>VRMs</h2>")
    parts.append(_table(
        ["VRM", "Type", "Group", "V set", "Load line (mΩ)", "V target", _vhead("V out", pdf),
         "V sense", "V gnd sense", "I out (A)",
         "V in", "I in (A)", "P out (W)", "P in (W)", "Loss (W)", _vhead("Rail lowest", pdf),
         "I limit (A)", "Result"],
        [[_e(v.name), _e(v.kind), _e(v.group), _num(v.v_set, ".5g"),
          _num(v.load_line_mohm or None, "g"), _num(v.v_target, ".5f"),
          _vcell(v.v_out, ref, pdf),
          _num(v.v_sense, ".5f"), _num(v.v_ground_sense, ".5f"), _num(v.i_out),
          _num(v.v_in, ".5f"), _num(v.i_in), _num(v.p_out), _num(v.p_in), _num(v.p_loss),
          _vcell(None if d is None else ref - d, ref, pdf),
          _num(v.current_limit_a, "g"), _verdict(v.passed)]
         for v in tree.vrms for d, _pct in [tree.rail_drop(v)]
         for ref in [v.v_set if tree.rail_target(v) is None else tree.rail_target(v)]]))
    parts.append("<p class='note'>Rail lowest: the lowest voltage anywhere on the copper of "
                 "the rail the VRM drives. V out and it are from the rail's target voltage, "
                 "the target V set less the load line's droop.</p>")
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
            title = f"<b>{_e(name)}</b>: {len(rows)} pin(s)"
            parts.append(
                (f"<h3>{title}</h3>" if pdf else f"<details><summary>{title}</summary>") +
                _table(["Terminal", "Role", "Pin", "x (mm)", "y (mm)", _vhead("V", pdf),
                        "I (mA)", "I is"],
                       [[_e(r.terminal), _e(r.role),
                         _e(f"{r.part}.{r.pin}" if r.part else r.pin), _num(r.x, ".3f"),
                         _num(r.y, ".3f"), _vcell(r.v, r.nominal, pdf),
                         _num(None if r.current is None else r.current * 1e3, ".4g"),
                         _e(r.current_is)] for r in rows]) +
                ("" if pdf else "</details>"))
        parts.append("<p class='note'>Every pin each group was solved with: VRM outputs, "
                     "phases, inputs and sense pins, load supply and ground pins and the pins "
                     "of DC connections. I is the current drawn from the copper at the pin; "
                     "negative is delivered into it (a VRM output, or a DC connection's far "
                     "side). VRM output pin currents are solved; a load's current is shared "
                     "equally by its pins, as the solver applies it, and a DC connection's by "
                     "the pins on each side. V is from the VRM target voltage (0 V for a "
                     "ground), or in volts.</p>")
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
        loads_w = getattr(th, "load_heat_w", {}) or {}
        with_loads = getattr(th.settings, "load_power", False)
        parts.append(f"<p>{th.heat_w:.3g} W into the board"
                     + (f" ({sum(max(v, 0.0) for v in loads_w.values()):.3g} W of it from the "
                        "loads)" if with_loads else ", load power not included")
                     + f"; peak <b>{tmax:.1f} °C</b> at "
                     f"({x:.1f}, {y:.1f}) mm{th.peak_layer(snap.stackup)}. Ambient "
                     f"{th.settings.ambient_c:g} °C, airflow "
                     f"{th.settings.airflow_m_s:g} m/s, {len(th.history)} iteration(s).</p>")
        if getattr(th, "heat_by_kind", None):
            parts.append(f"<p>Heat by source: {_e(th.heat_breakdown())}.</p>")
        heat_rows = ([[_e(k), "load", _num(v)] for k, v in loads_w.items()] if with_loads
                     else [])
        heat_rows += [[_e(s.name), "DC connection", _num(s.power)]
                      for g in tree.groups.values() for s in g.result.fields.series
                      if s.power > 0]
        if heat_rows:
            parts.append("<h3>Component power dissipation</h3>" + _table(
                ["Part", "Kind", "Power (W)"], heat_rows))
            parts.append("<p class='note'>Heat put into the board at each part's pins. A DC "
                         "connection dissipates I²R. "
                         + ("A load dissipates the sum over its supply and ground pins of pin "
                            "current × pin voltage." if with_loads else
                            "Load power is not included (Thermal settings tab).") + "</p>")
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
        parts.append(maps_html(_thermal_maps(th, snap, outline, pdf), pdf, large=True))
        if th.dielectrics:
            from kipi.dc.thermal.materials import coating_k
            coat = [[_e(c.name), _num(c.thickness_mm, ".3f"),
                     "solder mask" if c.kind == "mask" else "silkscreen, where it prints",
                     _num(coating_k(c), "g"), _num(coating_k(c), "g")]
                    for c in snap.stackup.coatings]
            n_top = sum(1 for c in snap.stackup.coatings if c.side == "top")
            parts.append("<h3>Laminate (thermal conductivity)</h3>" + _table(
                ["Stackup material", "Thickness (mm)", "Taken as", "In-plane k (W/m·K)",
                 "Through-plane k (W/m·K)"],
                coat[:n_top]
                + [[_e(m or "(unnamed)"), _num(t, ".3f"),
                    _e(i.matched) + (" ⚠" if i.assumed else ""), _num(i.k_w_mk, "g"),
                    _num(i.k_through, "g")]
                   for m, t, i in th.dielectrics]
                + coat[n_top:]))
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
        toggles = layer_toggles(f"plot{gi}", [names.get(lid, str(lid)) for lid in scene.layers],
                                bool(len(scene.via_segments)))
        if pdf:
            views.append((name, toggles + _plotly_3d(scene, snap, f"plot{gi}")))
            parts.append(f"<p class='note'>Interactive 3D view: {_views_link(path)}</p>")
        else:
            parts.append(toggles)
            parts.append(_plotly_3d(scene, snap, f"plot{gi}"))
            parts.append(f"<p class='note'>{_e(VIEW_NOTE)} The tickboxes hide or show a "
                         "layer in the view and its maps.</p>")
        rows = []
        tri_layer = g.result.mesh.tri_layer
        for lid, lr in scene.layers.items():
            v = lr.node_v[np.isfinite(lr.node_v)]
            sel = tri_layer == lid
            area = float(f.tri_area[sel].sum())
            j = lr.tri_j[np.isfinite(lr.tri_j)]
            rows.append([_e(names.get(lid, lid)), _num(area, ".1f"),
                         _vcell(float(v.min()) if len(v) else None, scene.v_ref, pdf),
                         _vcell(float(v.max()) if len(v) else None, scene.v_ref, pdf),
                         _num(np.percentile(j, 99.5) if len(j) else None, ".3g"),
                         _num(j.max() if len(j) else None, ".3g"),
                         _num(float(f.tri_power[sel].sum()) * 1e3, ".3f")])
        parts.append(_table(["Layer", "Copper (mm²)", _vhead("V min", pdf),
                             _vhead("V max", pdf), "|J| p99.5 (A/mm²)", "|J| max",
                             "Loss (mW)"], rows))
        parts.append(f"<p class='note'>V min and V max are from the {scene.v_ref:g} V "
                     + ("target voltage" if g.kind == "rail" else "ground reference")
                     + ", or in volts.</p>")
        maps, tags = [], []
        vmin, vmax = scene.ranges["voltage"]
        for lid, lr in scene.layers.items():
            lname = names.get(lid, str(lid))
            maps.append(_layer_map(lr, lr.node_v, False, f"{lname}: voltage", "turbo", "V",
                                   outline=outline, v_ref=scene.v_ref, pdf=pdf))
            maps.append(_layer_map(lr, lr.tri_j, True, f"{lname}: current density", "inferno",
                                   "A/mm²", clip_pct=99.5, outline=outline, pdf=pdf))
            tags += [(f"plot{gi}", lname)] * 2
        parts.append(maps_html(maps, pdf, tags, large=True))
        parts.append("<p class='note'>Current-density maps clip at the 99.5th percentile; pad "
                     "corners are singular and would otherwise wash out the map.</p>")

    if tree.warnings:
        parts.append("<h2>Notes</h2><ul>" + "".join(f"<li>{_e(w)}</li>" for w in tree.warnings)
                     + "</ul>")

    parts.append(notes_section(pdf))             # last, after every result
    title = f"KiPI report — {snap.board_name}"
    attachments = [(views_name(path), views_page(f"KiPI 3D views — {snap.board_name}",
                                                 f"{meta} · from {_e(path.name)}", views),
                    "Interactive 3D views of the report's net groups")] if views else []
    finish(path, title, parts, CSS, pdf, attachments)
    info(f"Report written: {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return path
