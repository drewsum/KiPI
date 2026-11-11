# Portions Copyright (c) 2026 Tomachie LLC.
"""Numbers out of a power-tree file's value fields and datasheet figures.

A value field holds a number or a word ("unknown", "not_specified"); a current is an
object with ``amps`` or one of those words. Datasheet figures are printed strings.

A series part's DC resistance comes only from figures that ARE a DC resistance. A
ferrite's datasheet also prints its impedance in ohms, at 100 MHz; taking that for its DC
resistance would be wrong by orders of magnitude, so figures are chosen by what they
are, never by their unit alone.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

DC_RESISTANCE_SPECS = ("dcr", "dc_resistance", "resistance", "rds_on", "rdson", "ron", "r_on",
                       "jumper_resistance", "cold_resistance", "r_dc", "rdc", "on_resistance",
                       "contact_resistance")
# A 0 ohm jumper's own figure: the most it may measure.
JUMPER_SPECS = ("jumper_resistance",)
# The prefixes of a DC-resistance spec that name a part's own pass element
# ("switch_on_resistance", "channel_resistance", "pass_fet_on_resistance") rather than
# another pin's FET ("flag_on_resistance", "discharge_resistance").
OWN_SWITCH_PREFIXES = ("switch", "channel", "pass", "pass_fet", "fet", "main", "output")
OHMS_PER_UNIT = {"ohm": 1.0, "ohms": 1.0, "Ω": 1.0, "mohm": 1e-3, "mΩ": 1e-3,
                 "kohm": 1e3, "kΩ": 1e3, "uohm": 1e-6, "µΩ": 1e-6}
_NUMBER = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def number(value) -> Optional[float]:
    """A value field as a float; None for a word such as "unknown"."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def amps(current) -> Optional[float]:
    """A current object's amps; None for "unknown" or a missing current."""
    return number(current.get("amps")) if isinstance(current, dict) else None


def _printed(text) -> Optional[float]:
    m = _NUMBER.search(str(text or ""))
    return float(m.group()) if m else None


def _ohms(fig: dict, column: str) -> Optional[float]:
    unit = str(fig.get("unit", "")).strip()
    # Exact first: lower-casing turns the ohm sign into a Greek small omega.
    scale = OHMS_PER_UNIT.get(unit, OHMS_PER_UNIT.get(unit.lower()))
    value = _printed(fig.get(column))
    return None if value is None or scale is None else abs(value) * scale


def _prefix(spec, names=DC_RESISTANCE_SPECS) -> Optional[str]:
    """"" when a figure's spec is one of ``names``; the prefix when it is one after a
    prefix ("BLM18PG121SH1_dc_resistance": a series datasheet's figure for one part of
    it), never the bare "resistance" after one ("insulation_resistance"); else None."""
    spec = str(spec or "").lower()
    if spec in names:
        return ""
    for s in names:
        if s != "resistance" and spec.endswith("_" + s):
            return spec[:-len(s) - 1]
    return None


def _is_dc_resistance(spec, names=DC_RESISTANCE_SPECS) -> bool:
    return _prefix(spec, names) is not None


def _own_figures(element: dict) -> List[dict]:
    """The DC-resistance figures for this part: a figure named after something else (a
    load switch's flg_on_resistance, its FLAG pin's FET) is left out when one is named
    for the part itself (rds_on), and a series datasheet's figures named after another
    part of the series are left out when one is named after this part."""
    figs = [(f, _prefix(f.get("spec"))) for f in element.get("datasheet_figures", [])]
    figs = [(f, p) for f, p in figs if p is not None]
    if any(not p for _, p in figs):
        return [f for f, p in figs if not p]
    pn = str(element.get("part_number") or "").lower()
    if len({p for _, p in figs if p}) > 1 and pn:
        own = [(f, p) for f, p in figs if not p or p in pn or pn in p]
        figs = own if any(p for _, p in own) else figs
    if len({p for _, p in figs if p}) > 1:
        # Several prefixes and none is the part number: the ones that name the part's
        # own pass element win over another pin's FET (2026-11-02, AP22615: the
        # "switch_on_resistance" 40 mOhm beside "flag_on_resistance" 40 ohm; the flag
        # figure had been taken and five USB rails solved to -117 V).
        own = [(f, p) for f, p in figs if p in OWN_SWITCH_PREFIXES]
        figs = own if own else figs
    return [f for f, _ in figs]


def dc_resistance(element: dict) -> Tuple[Optional[float], Optional[float], List[str]]:
    """(typical, maximum, notes) of a series part's DC resistance in ohms.

    A resistor's own value is its resistance; a 0 ohm one's maximum is its datasheet's
    jumper resistance, where printed. Otherwise the DC-resistance figures of its
    datasheet: where several are printed (different conditions) the highest is used and
    said so."""
    own = number(element.get("resistance_ohm"))
    if own is not None:
        if own != 0:
            return own, None, []
        jumper = [v for v in (_ohms(f, c) for f in element.get("datasheet_figures", [])
                              if _is_dc_resistance(f.get("spec"), JUMPER_SPECS)
                              for c in ("max", "typ")) if v is not None]
        return 0.0, (max(jumper) if jumper else None), []
    figs = _own_figures(element)
    typs = [v for v in (_ohms(f, "typ") for f in figs) if v is not None]
    maxs = [v for v in (_ohms(f, "max") for f in figs) if v is not None]
    notes = []
    if len(set(typs)) > 1 or len(set(maxs)) > 1:
        notes.append("several DC-resistance figures printed; the highest of each is used")
    return (max(typs) if typs else None), (max(maxs) if maxs else None), notes


# A diode's forward-voltage figures, under whatever key the datasheet read gave them.
_FORWARD_V_SPEC = re.compile(r"(?i)^(v_?f\w*|v_?forward\w*|forward_voltage\w*)$")
_AMPS_PER_UNIT = {"a": 1.0, "ma": 1e-3, "ua": 1e-6, "µa": 1e-6}
# A series part's continuous current rating, under the names a datasheet read gives it:
# a ferrite's, inductor's, fuse's or relay contact's rated current, a resettable fuse's
# hold current, a 0 ohm jumper's rated (not its peak) current. Peak, pulsed, trip and
# leakage figures are not ratings to carry current at.
RATED_CURRENT_SPECS = ("rated_current", "i_hold", "hold_current", "jumper_rated_current",
                       "continuous_drain_current", "average_forward_current", "if_av")


def rated_current(element: dict) -> Optional[float]:
    """The series part's continuous rated current in amps, the lowest printed where several
    are (a rating at temperature next to one at 25 C); None when none is printed."""
    out = []
    for f in element.get("datasheet_figures", []) or []:
        if str(f.get("spec", "")).lower() not in RATED_CURRENT_SPECS:
            continue
        unit = str(f.get("unit", "")).strip().split()[0].lower() if f.get("unit") else ""
        scale = _AMPS_PER_UNIT.get(unit)
        value = _printed(f.get("max")) if f.get("max") not in (None, "") else _printed(f.get("typ"))
        if scale is not None and value is not None and value > 0:
            out.append(value * scale)
    return min(out) if out else None


def _test_amps(text) -> Optional[float]:
    """The test current printed with a figure ("5A", "500 mA"), in amps."""
    m = re.match(r"\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*([mµu]?A)\b", str(text or ""))
    return float(m.group(1)) * _AMPS_PER_UNIT[m.group(2).lower()] if m else None


def forward_drop(element: dict) -> Tuple[Optional[float], Optional[float], float, List[str]]:
    """(forward voltage, its maximum, slope resistance in ohms, notes) of a series diode
    from its datasheet's forward-voltage figures; (None, None, 0, []) when none is printed.

    Two figures at different test currents give a drop and a slope (V = Vf + I x R),
    fitted on the typical figures, else the maximum ones. One figure is a constant drop:
    at currents below its test current the diode drops less, so it reads high there."""
    figs = [f for f in element.get("datasheet_figures", [])
            if _FORWARD_V_SPEC.match(str(f.get("spec", "")))
            and str(f.get("unit", "")).strip().upper() == "V"]
    cols = {c: [(_test_amps(f.get("if")), _printed(f.get(c))) for f in figs] for c in ("typ", "max")}
    cols = {c: [(i, v) for i, v in pts if v is not None] for c, pts in cols.items()}
    fitted = _curve_fit(element)
    if fitted is not None:
        drop, slope, how = fitted
        # The curve is typical; a printed maximum lifts it for the worst case.
        vf_max = None
        if cols["max"]:
            i_m, v_m = max(cols["max"], key=lambda p: p[1])
            vf_max = max(v_m - (i_m or 0.0) * slope, drop)
        return drop, vf_max, slope, [how]
    if not cols["typ"] and not cols["max"]:
        return None, None, 0.0, []
    fit_col = "typ" if cols["typ"] else "max"
    pts = sorted((i, v) for i, v in cols[fit_col] if i is not None)
    if len(pts) >= 2 and pts[-1][0] > pts[0][0]:
        (i1, v1), (i2, v2) = pts[0], pts[-1]
        slope = max((v2 - v1) / (i2 - i1), 0.0)
        drop = v1 - i1 * slope
        how = (f"forward drop {drop:.3g} V + {slope * 1e3:.3g} mOhm slope, from the "
               f"{fit_col} figures at {i1:g} A and {i2:g} A")
    else:
        i_at, drop = max(cols[fit_col], key=lambda p: p[1])
        slope = 0.0
        at = f" at {i_at:g} A" if i_at is not None else ""
        how = (f"forward drop {drop:g} V ({fit_col}{at}) as a constant: below that "
               "current it drops less, so the rail after it reads somewhat low")
    vf_max = None
    if fit_col == "typ" and cols["max"]:
        vf_max = drop + max(v for _, v in cols["max"]) - max(v for _, v in cols["typ"])
    return drop, vf_max, slope, [how]


_AT_25C = re.compile(r"(?<![\d.])25\s*°?\s*C\b", re.I)


def _curve(element: dict) -> Optional[dict]:
    """The element's Vf-vs-If curve to solve on (``vf_curves``): the 25 C one if one is
    labelled so, else the first; None without one."""
    curves = [c for c in element.get("vf_curves") or [] if len(c.get("points") or []) >= 2]
    if not curves:
        return None
    return next((c for c in curves if _AT_25C.search(str(c.get("label", "")))), curves[0])


def curve_points(element: dict) -> Optional[List[List[float]]]:
    """That curve's points, [[amps, volts], ...]; None without one."""
    curve = _curve(element)
    return None if curve is None else [[float(i), float(v)] for i, v in curve["points"]]


def _curve_fit(element: dict):
    """(drop, slope, how) fitted on the element's Vf-vs-If curve (:func:`_curve`): a
    straight line through the points above a tenth of its highest current, where the
    diode conducts; the solve then follows the curve itself. None without a curve."""
    curve = _curve(element)
    if curve is None:
        return None
    pts = sorted((float(i), float(v)) for i, v in curve["points"])
    top = pts[-1][0]
    on = [(i, v) for i, v in pts if i >= 0.1 * top] or pts
    if len(on) < 2:
        on = pts[-2:]
    n = len(on)
    mi = sum(i for i, _ in on) / n
    mv = sum(v for _, v in on) / n
    sxx = sum((i - mi) ** 2 for i, _ in on)
    slope = max(sum((i - mi) * (v - mv) for i, v in on) / sxx, 0.0) if sxx > 0 else 0.0
    drop = mv - slope * mi
    label = f" ({curve['label']})" if curve.get("label") else ""
    page = f", page {curve['page']}" if curve.get("page") else ""
    how = (f"solved on its Vf-vs-If curve{label}{page} at the current it carries "
           f"(slope {slope * 1e3:.3g} mOhm, from {on[0][0]:g} A to {on[-1][0]:g} A)")
    return drop, slope, how
