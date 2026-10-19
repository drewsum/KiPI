"""HTML (or PDF) report of a Coupled Noise solve (#69).

One file with the aggressor's operating point and parasitics, its waveforms, each
victim's noise against its margin (with the mechanisms apart), the scan's estimates and
the notes. Plots are images, the same in the web page and the PDF; the layout and styling
are the DC and AC reports'.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import List, Optional

import numpy as np

from kipi import __version__
from kipi.core.naming import timestamp
from kipi.dc.report.html import CSS, _e, _png, _table, _verdict, finish, notes_section

COLORS = ("#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#17becf")
MAX_VICTIM_PLOTS = 8


def report_name(board_stem: str, aggressor: str, when: Optional[_dt.datetime] = None,
                fmt: str = "html") -> str:
    """'<board>_<aggressor>_noise_report_<time stamp>.html' (or .pdf)."""
    safe = re.sub(r"[^\w.+\-]+", "_", aggressor).strip("_") or "aggressor"
    return f"{board_stem}_{safe}_noise_report_{timestamp(when)}.{fmt}"


def _figure():
    from matplotlib.figure import Figure
    fig = Figure(figsize=(9, 3.2))
    return fig, fig.add_subplot(111)


def _time_png(curves, wf, title: str, unit: str, margin: Optional[float] = None) -> str:
    fig, ax = _figure()
    periods = 2
    t = wf.time(periods) * 1e6
    n = wf.samples_per_period()
    for i, (label, sp) in enumerate(curves):
        ax.plot(t, sp.samples(n, periods), lw=0.9, color=COLORS[i % len(COLORS)],
                label=label)
    if margin is not None:
        for sign in (1, -1):
            ax.axhline(sign * margin, color="#d62728", lw=0.8, ls="--",
                       label="noise margin" if sign > 0 else None)
    ax.set_xlabel("Time (µs)")
    ax.set_ylabel(unit)
    ax.set_title(title, fontsize=10)
    ax.grid(True, lw=0.4, color="#ccc")
    if curves:
        ax.legend(fontsize=7, loc="best")
    fig.tight_layout()
    return _png(fig)


def _img(src: str) -> str:
    return f"<p><img src='{src}' style='max-width:100%'></p>"


def write_noise_report(path, result, snap, setup, progress=None) -> Path:
    """Write the report of ``result`` (a :class:`~kipi.noise.result.NoiseResult`): a web
    page, or a PDF when ``path`` ends in .pdf."""
    path = Path(path)
    pdf = path.suffix.lower() == ".pdf"
    wf = result.waveforms
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    parts: List[str] = [
        f"<h1>KiPI Coupled Noise report: {_e(result.aggressor)}</h1>"
        f"<div class='meta'>{_e(snap.board_name)} · {now} · KiPI {__version__}</div>"]
    if not pdf:
        parts.append("<nav><a href='#victims'>Victims</a> <a href='#aggressor'>Aggressor</a> "
                     "<a href='#waveforms'>Waveforms</a> <a href='#noise'>Victim noise</a>"
                     + (" <a href='#scan'>Scan</a>" if result.scan else "")
                     + " <a href='#notes'>Notes</a> <a href='#user-notes'>User notes</a></nav>")
    judged = [v.passed for v in result.victims]
    if judged:
        parts.append(f"<p>Overall: {_verdict(all(judged))} ({sum(judged)} of {len(judged)} "
                     "victim(s) within their noise margin).</p>")

    # ---------------- victims
    parts.append("<h2 id='victims'>Victims</h2>")
    if result.victims:
        rows = []
        for v in result.victims:
            by = ", ".join(f"{k} {p * 1e3:.3g}" for k, p in v.peak_by.items())
            rows.append([_e(v.name) + (" (scan)" if v.scanned else ""), _e(v.net),
                         _e(f"{v.driver} → {v.receiver}"), f"{v.peak_v * 1e3:.4g}",
                         f"{v.margin_v * 1e3:.4g}", f"{v.ratio * 100:.3g} %", _e(by),
                         _e(v.dominant), _verdict(v.passed)])
        parts.append(_table(["Victim", "Net", "Driver → receiver", "Peak (mV)",
                             "Margin (mV)", "Of margin", "By mechanism (mV)", "Dominant",
                             "Result"], rows))
        parts.append("<p class='note'>Peak: the largest noise at the receiver over a "
                     "switching period, all mechanisms added as phasors. Scanned nets use "
                     "the scan's default driver and logic level.</p>")
    else:
        parts.append("<p>No victims were solved.</p>")

    # ---------------- aggressor
    parts.append("<h2 id='aggressor'>Aggressor</h2>")
    parts.append("<ul>" + "".join(f"<li>{_e(x)}</li>" for x in result.summary_lines()
                                  if not x.startswith("Victim ")) + "</ul>")
    rows = [[str(h.phase), f"{h.loop_l_h * 1e9:.3g}", f"{h.c_sw_f * 1e12:.4g}",
             f"{h.coss_f * 1e12:.4g}" + (" (assumed)" if h.coss_default else ""),
             f"{h.c_copper_f * 1e12:.3g}", f"{h.r_loop_ohm * 1e3:.3g}",
             f"{h.f_ring_hz / 1e6:.4g}" if h.f_ring_hz else "", f"{h.zeta:.3g}",
             f"{h.qrr_c * 1e9:.3g}" + (" (assumed)" if h.qrr_default else "")]
            for h in result.hot_loops]
    parts.append(_table(["Phase", "Hot loop (nH)", "Switch node C (pF)", "Coss (pF)",
                         "Copper C (pF)", "Loop R (mΩ)", "Ringing (MHz)", "ζ (hard edge)",
                         "Qrr (nC)"], rows))

    # ---------------- waveforms
    if progress is not None:
        progress.info("Report: waveforms")
    parts.append("<h2 id='waveforms'>Waveforms</h2>")
    parts.append(_img(_time_png([(p.switch_node.name, p.switch_node) for p in wf.phases], wf,
                                "Switch node", "V")))
    parts.append(_img(_time_png([(p.inductor.name, p.inductor) for p in wf.phases], wf,
                                "Inductor currents", "A")))
    for caps, title in ((wf.input_caps, "Input capacitor currents"),
                        (wf.output_caps, "Output capacitor currents")):
        if caps:
            top = sorted(caps.items(), key=lambda kv: -kv[1].ac_rms())[:6]
            parts.append(_img(_time_png([(f"{r} ({s.ac_rms():.3g} A rms)", s) for r, s in top],
                                        wf, title, "A")))
    ripple = [(s.name, s) for s in (wf.input_ripple, wf.output_ripple) if s is not None]
    if ripple:
        parts.append(_img(_time_png(ripple, wf, "Rail ripple", "V")))
    probes = getattr(wf, "probe_ripple", {})
    if probes:
        parts.append(_img(_time_png([(f"{n} (AC probe)", s) for n, s in probes.items()], wf,
                                    "Ripple at AC probes", "V")))
        n = wf.samples_per_period()
        parts.append(_table(["AC probe", "Ripple (mV p-p)", "Ripple (mV rms)"],
                            [[_e(name), f"{np.ptp(s.samples(n)) * 1e3:.4g}",
                              f"{s.ac_rms() * 1e3:.3g}"] for name, s in probes.items()]))

    # ---------------- victim noise
    parts.append("<h2 id='noise'>Victim noise</h2>")
    worst = sorted(result.victims, key=lambda v: -v.ratio)[:MAX_VICTIM_PLOTS]
    for v in worst:
        curves = [(f"{v.receiver} total", v.receiver_v["total"])] + \
            [(k, sp) for k, sp in v.receiver_v.items() if k != "total"]
        margin = v.margin_v if v.margin_v <= 4 * max(v.peak_v, 1e-15) else None
        parts.append(f"<h3>{_e(v.name)}</h3>")
        parts.append(_img(_time_png(curves, wf, f"{v.name}: {v.peak_v * 1e3:.4g} mV peak, "
                                    f"{v.ratio * 100:.3g} % of its margin", "V", margin)))
    if len(result.victims) > len(worst):
        parts.append(f"<p class='note'>The {len(worst)} victims nearest their margin are "
                     "plotted.</p>")

    # ---------------- scan
    if result.scan:
        parts.append("<h2 id='scan'>Scan</h2>")
        rows = [[_e(r.net), f"{r.distance_mm:.3g}", f"{r.estimate_v * 1e3:.4g}",
                 f"{r.capacitive_v * 1e3:.4g}", f"{r.inductive_v * 1e3:.4g}",
                 "yes" if r.solved else ""] for r in result.scan]
        parts.append(_table(["Net", "Distance (mm)", "Estimate (mV)", "Capacitive (mV)",
                             "Inductive (mV)", "Solved in full"], rows))
        parts.append("<p class='note'>The estimate ranks the nets near the aggressor quickly; "
                     "the worst are solved in full (above, marked scan).</p>")

    # ---------------- notes
    warnings = list(result.warnings) + [f"{v.name}: {w}" for v in result.victims
                                        for w in v.warnings]
    parts.append("<h2 id='notes'>Notes</h2>")
    parts.append("<ul>" + "".join(f"<li>{_e(w)}</li>" for w in warnings) + "</ul>"
                 if warnings else "<p>None.</p>")
    parts.append(notes_section(pdf))
    return finish(path, f"KiPI Coupled Noise report: {result.aggressor}", parts, CSS, pdf)
