"""The field viewer, the inductor's stray field, plane-split crossings and the near-field
report (#146): the field formulas against closed forms, the sources a solve builds, the
overlays, and their place in the panel, the 3D view and the window."""

import math
import os

import numpy as np
import pytest

from kipi.ac.model import CapModel
from kipi.core.model.board import Poly
from kipi.noise.checks import seed_phases
from kipi.noise.extract import peec
from kipi.noise.extract.capacitive import Segment
from kipi.noise.fields import (
    ESource, FieldSlice, FieldSources, HSource, biot_savart, near_field_report, patch_field,
)
from kipi.noise.model import Aggressor, AggressorPhase, Victim, phase_from_dict
from kipi.noise.network import Termination, VictimNetwork
from kipi.noise.parasitics import _Board, inductor_loop
from kipi.noise.solver import solve_noise
from kipi.noise.splits import plane_splits, slot_inductance
from kipi.noise.viz import field_choices, field_overlay
from kipi.noise.waveforms import Spectrum
from noise_boards import buck_board, buck_setup
from synthetic import F, IN1, rect

MU0 = 4e-7 * math.pi


def _setup(snap, victims=(), shielded=True, board_model=False):
    setup = buck_setup(board_model=board_model)
    vrm = next(v for v in setup.vrms if v.name == "U1")
    a = Aggressor("U1 buck", "U1", fsw_hz=1e6)
    a.phases = seed_phases(snap, setup, vrm, "buck")
    a.phases[0].coss_f = 300e-12
    a.phases[0].inductor_shielded = shielded
    setup.aggressors.append(a)
    setup.caps = [CapModel("10u", 10e-6, 0.4e-9, 0.003, ["C1"]),
                  CapModel("47u", 47e-6, 0.6e-9, 0.002, ["C2"])]
    setup.victims = list(victims)
    return setup


# ------------------------------------------------------------------ the formulas
def test_a_long_wire_gives_i_over_two_pi_r():
    a = np.array([[-500.0, 0, 0]])
    b = np.array([[500.0, 0, 0]])
    pts = np.array([[0, 2.0, 0], [0, 0, 5.0], [3.0, -1.0, 0]])
    h = biot_savart(a, b, np.array([2.0]), np.array([0.01]), pts)
    for p, hv in zip(pts, h):
        r = math.hypot(p[1], p[2]) * 1e-3
        assert np.linalg.norm(hv) == pytest.approx(2.0 / (2 * math.pi * r), rel=1e-3)
    # Right-hand rule: current along +x, at +y the field points +z.
    assert h[0][2] > 0 and abs(h[0][0]) < 1e-9 and abs(h[0][1]) < 1e-9


def test_a_square_loops_centre_field_and_moment():
    """H at the centre of a square loop of side a is 2√2·I/(π·a); its moment is I·a²."""
    s = 4.0
    pts = np.array([[0, 0, 0], [s / 2, 0, 0], [s / 2, s / 2, 0], [0, s / 2, 0]], float) - [s / 4, s / 4, 0]
    corners = [[-2, -2, 0], [2, -2, 0], [2, 2, 0], [-2, 2, 0]]
    segs = peec.polyline(corners, [0.05] * 4, 0.035)
    src = HSource("L", "L", 1, np.array([x.a for x in segs]), np.array([x.b for x in segs]),
                  np.ones(4), np.array([x.radius for x in segs]), None, 0.035, s,
                  Spectrum("I", "A", 0.0, np.array([1.0 + 0j])))
    h = src.maps(np.array([[0.0, 0.0, 0.0]]), 1)["direct"][0]
    assert abs(h[2]) == pytest.approx(2 * math.sqrt(2) / (math.pi * s * 1e-3), rel=1e-3)
    m = src.moments()["direct"]
    assert abs(m[2]) == pytest.approx((s * 1e-3) ** 2, rel=1e-9) and abs(m[0]) < 1e-15
    assert pts.shape == (4, 3)


def test_a_loop_over_a_plane_has_its_image_cancel_its_moment():
    """Horizontal loop over a plane: the image's moment is the opposite, so at high
    frequency (Γ → 1) the pair radiates far less than the loop alone."""
    corners = [[-2, -2, 1.0], [2, -2, 1.0], [2, 2, 1.0], [-2, 2, 1.0]]
    segs = peec.polyline(corners, [0.05] * 4, 0.035)
    src = HSource("L", "L", 1, np.array([x.a for x in segs]), np.array([x.b for x in segs]),
                  np.ones(4), np.array([x.radius for x in segs]), (0.0, 0.035), 0.035, 4.0,
                  Spectrum("I", "A", 0.0, np.array([1.0 + 0j])))
    m = src.moments()
    assert np.allclose(m["direct"] + m["image"], 0, atol=1e-15)
    # The image field above the plane opposes the loop's.
    pt = np.array([[0.0, 0.0, 3.0]])
    maps = src.maps(pt, 1)
    assert maps["direct"][0, 2] * maps["image"][0, 2] < 0
    w = src.weights_at(np.array([1e3, 1e9]))
    assert abs(w["image"][0]) < 0.05 and abs(w["image"][1]) > 0.99


def test_a_patch_over_a_plane_fields_like_a_dipole():
    """A charge and its image: E on the axis far away is 2·q·d/(4πε0·r³)."""
    q = 1e-12
    src = ESource("SW", "SW", 1, np.array([[0.0, 0.0, 0.5]]), np.array([q]), np.array([0.0]),
                  0.2, Spectrum("V", "V", 0.0, np.array([1.0 + 0j])))
    e = src.map(np.array([[0.0, 0.0, 50.0]]))[0]
    r, d = 50e-3, 1.0e-3                                    # the image sits 1 mm below
    expect = 2 * q * d / (4 * math.pi * 8.8541878128e-12 * r ** 3)
    assert e[2] == pytest.approx(expect, rel=0.01) and abs(e[0]) < 1e-12
    single = patch_field(np.array([[0.0, 0.0, 0.0]]), np.array([q]), 0.2,
                         np.array([[0.0, 0.0, 10.0]]))[0]
    assert single[2] == pytest.approx(q / (4 * math.pi * 8.8541878128e-12 * 0.01 ** 2), rel=1e-3)


# ------------------------------------------------------------------ the inductor
def _phase(snap, shielded, axis="vertical"):
    ph = AggressorPhase(inductor="L1", inductance_h=1e-6, inductor_shielded=shielded,
                        inductor_axis=axis)
    return inductor_loop(_Board(snap, 4.3), ph)


def test_the_inductor_is_a_loop_of_its_turns():
    snap = buck_board()
    loop, note = _phase(snap, shielded=False)
    assert len(loop.elements) == 4 and "unshielded" in note and "vertical" in note
    # L1's pads span x 15.5-20.5: the body is taken 5 x 3 mm, the winding 1.8 mm across,
    # with N = √(L·h/(μ0·a²)) turns.
    a, h = 0.6 * 3.0, 0.4 * 3.0
    turns = math.sqrt(1e-6 * h * 1e-3 / (MU0 * (a * 1e-3) ** 2))
    assert loop.weights[0] == pytest.approx(turns, rel=1e-6)
    assert loop.size_mm == pytest.approx(a)
    centre = loop.paths[0][:-1].mean(axis=0)
    assert np.allclose(centre[:2], (18.0, 12.0)) and centre[2] > 1.27
    assert all(abs(e.a[2] - centre[2]) < 1e-9 for e in loop.elements)     # flat: vertical axis
    shielded, note = _phase(snap, shielded=True)
    assert shielded.weights[0] < 0.2 * loop.weights[0] and shielded.size_mm < loop.size_mm
    assert "shielded" in note and "tenth" in note
    # Lying along the part, the loop stands up, normal to x.
    along, note = _phase(snap, shielded=False, axis="along")
    assert "along the part (x)" in note
    xs = {round(float(e.a[0]), 6) for e in along.elements}
    assert xs == {18.0} and len({round(float(e.a[2]), 6) for e in along.elements}) == 2
    assert inductor_loop(_Board(snap, 4.3), AggressorPhase(inductor="L1")) == (None, "")


def test_a_shielded_inductors_field_falls_off_sooner():
    """At the body the shielded part's field is weaker; two body widths away it is
    weaker by a larger factor still, its loop being the smaller."""
    snap = buck_board()
    fields = {}
    for shielded in (False, True):
        loop, _ = _phase(snap, shielded)
        a = np.array([e.a for e in loop.elements])
        b = np.array([e.b for e in loop.elements])
        r = np.array([e.radius for e in loop.elements])
        pts = np.array([[18.0, 12.0, 4.0], [18.0, 12.0, 10.0]])
        fields[shielded] = np.linalg.norm(biot_savart(a, b, loop.weights, r, pts), axis=1)
    near = fields[True][0] / fields[False][0]
    far = fields[True][1] / fields[False][1]
    assert near < 0.2 and far < near


def test_the_winding_is_saved_and_a_source():
    d = {"inductor": "L1", "inductance_h": 1e-6, "inductor_axis": "across",
         "inductor_shielded": False}
    ph = phase_from_dict(d, lambda x: None)
    assert (ph.inductor_axis, ph.inductor_shielded) == ("across", False)
    assert phase_from_dict({"inductor_axis": "sideways"}, lambda x: None).inductor_axis == "vertical"
    assert phase_from_dict({}, lambda x: None).inductor_shielded is True
    snap = buck_board(victim_gap=0.2)
    setup = _setup(snap, [Victim("SIG")], shielded=False)
    r = solve_noise(snap, setup, "U1 buck")
    assert "L1 field (phase 1)" in r.victims[0].m_h
    assert any("L1: unshielded" in w for w in r.warnings)
    keys = [s.key for s in r.fields.h]
    assert keys == ["C1 loop (phase 1)", "C2 loop (phase 1)", "L1 field (phase 1)"]
    assert [s.key for s in r.fields.e] == ["SW (phase 1)"]
    ind = r.fields.get("L1 field (phase 1)")
    assert ind.current.mean == pytest.approx(5.0, rel=1e-6) and len(ind.a) == 4
    assert ind.plane == (1.2525, 1.27) and ind.sides().tolist() == [1, 1, 1, 1]
    sw = r.fields.e[0]
    assert len(sw.xyz) > 50 and np.all(sw.q_per_v > 0) and np.all(np.isfinite(sw.image_z))
    # Every patch's charge is its capacitance to the plane 0.2 mm under the 1 mm pour.
    assert sw.q_per_v.sum() == pytest.approx(8.8541878128e-15 * 4.3 * 16.0 / 0.2, rel=0.1)


# ------------------------------------------------------------------ the overlay, the report
@pytest.fixture(scope="module")
def solved():
    snap = buck_board(victim_gap=0.2)
    setup = _setup(snap, [Victim("SIG")], shielded=False)
    return snap, setup, solve_noise(snap, setup, "U1 buck")


def test_the_field_overlay_is_a_slice_over_the_sources(solved):
    _, _, r = solved
    assert field_overlay(r, "off", ["C1 loop (phase 1)"]) is None
    assert field_overlay(r, "H", ["nope"]) is None and field_overlay(None, "H", []) is None
    assert field_choices(r, "H") == [("C1 loop (phase 1)", "C1 loop"),
                                     ("C2 loop (phase 1)", "C2 loop"),
                                     ("L1 field (phase 1)", "L1 field")]
    assert field_choices(r, "E") == [("SW (phase 1)", "SW switch node")]
    ov = field_overlay(r, "H", ["C1 loop (phase 1)"], height_mm=2.0)
    assert ov.unit == "A/m" and ov.points.shape == (ov.nx * ov.ny, 3)
    assert np.allclose(ov.points[:, 2], r.fields.z_top_mm + 2.0) and r.fields.z_top_mm > 1.27
    assert ov.magnitude.shape == (len(ov.points),) and ov.vectors.shape == (len(ov.points), 3)
    assert ov.limits[1] == pytest.approx(ov.magnitude.max()) and ov.limits[0] == ov.limits[1] / 1e3
    assert np.allclose(np.linalg.norm(ov.vectors, axis=1), ov.magnitude, rtol=1e-6)
    assert ov.frames is None and "C1 loop" in ov.note and "2 mm" in ov.note
    # The slice covers the loop (C1 at x 6, U1 at x 10) with room around it.
    assert ov.points[:, 0].min() < 6 - 2 and ov.points[:, 0].max() > 10 + 2
    # The peak sits over the loop, not out at the edge.
    i = int(np.argmax(ov.magnitude))
    assert 4 < ov.points[i, 0] < 12 and 8 < ov.points[i, 1] < 16
    anim = field_overlay(r, "H", ["C1 loop (phase 1)", "L1 field (phase 1)"], animate=True)
    assert anim.frames.shape == (96, len(anim.points), 3) and anim.frames.dtype == np.float32
    # No frame beats the peak map; the DC of the inductor's current is left out.
    assert np.linalg.norm(anim.frames, axis=2).max() <= anim.limits[1] * 1.001
    e = field_overlay(r, "E", ["SW (phase 1)"])
    assert e.unit == "V/m" and e.magnitude.max() > 0 and "SW switch node" in e.note


def test_the_slice_adds_the_sources_fields(solved):
    _, _, r = solved
    fs, n = r.fields, r.waveforms.samples_per_period()
    one = FieldSlice(fs, "H", [fs.h[0]], 2.5, n, cell_mm=1.0, pad_mm=2.0)
    two = FieldSlice(fs, "H", [fs.h[1]], 2.5, n, cell_mm=1.0, pad_mm=2.0)
    both = FieldSlice(fs, "H", fs.h[:2], 2.5, n, cell_mm=1.0, pad_mm=2.0)
    t = [0, 40, 100]
    # The grids differ (each covers its own sources): compare at a shared point.
    p = np.array([[8.0, 12.0, 2.5]])
    a = FieldSlice(fs, "H", [fs.h[0]], 2.5, n, cell_mm=1.0, pad_mm=0.0)
    assert a.pts.shape[1] == 3 and both.maps.shape[0] == one.maps.shape[0] + two.maps.shape[0]
    f1 = np.einsum("mt,mpk->tpk", one.waves[:, t], np.stack([fs.h[0].maps(p, 1)[k] for k in
                                                              fs.h[0].maps(p, 1)]))
    f2 = np.einsum("mt,mpk->tpk", two.waves[:, t], np.stack([fs.h[1].maps(p, 1)[k] for k in
                                                              fs.h[1].maps(p, 1)]))
    f12 = np.einsum("mt,mpk->tpk", both.waves[:, t], np.stack(
        [m for s in fs.h[:2] for m in s.maps(p, 1).values()]))
    assert np.allclose(f12, f1 + f2)


def test_the_near_field_report_finds_the_loops_and_the_switch_node(solved):
    _, _, r = solved
    emi = r.emi
    assert emi.height_mm == 5.0 and emi.distance_m == 3.0
    assert emi.h_hotspots and emi.e_hotspots
    h, e = emi.h_hotspots[0], emi.e_hotspots[0]
    assert h.unit == "A/m" and e.unit == "V/m" and h.value > 0 and e.value > 0
    assert h.source in ("C1 loop", "C2 loop", "L1 field") and e.source == "SW switch node"
    assert 11 < e.x_mm < 16 and 9 < e.y_mm < 15                    # over the SW pour
    assert all(math.hypot(a.x_mm - b.x_mm, a.y_mm - b.y_mm) >= 3.0
               for i, a in enumerate(emi.h_hotspots) for b in emi.h_hotspots[i + 1:])
    assert emi.radiated_f.shape == emi.radiated_dbuv.shape == emi.limit_dbuv.shape
    assert np.isnan(emi.limit_dbuv[0]) and emi.limit_dbuv[np.argmin(abs(emi.radiated_f - 100e6))] == 40
    assert 30e6 <= emi.worst_f_hz < 1e9 and np.isfinite(emi.worst_dbuv)
    assert emi.worst_limit_dbuv in (40.0, 47.0) and emi.worst_source
    lines = emi.lines()
    assert len(lines) == 3 and "CISPR" in lines[2] and "strongest H" in lines[0]
    assert any("cables" in n for n in emi.notes)
    assert any("Near field" in x for x in r.summary_lines())

    # Each hotspot is a peak of its own, not the strongest one's shoulder: the inductor's
    # and C1's loop, each once; the switch node's one pour, once.
    assert [h.source for h in emi.h_hotspots] == ["L1 field", "C1 loop"]
    assert len(emi.e_hotspots) == 1


def test_hotspots_are_separate_peaks():
    from kipi.noise.fields import _peaks
    x, y = np.meshgrid(np.arange(20.0), np.arange(10.0))
    bump = lambda x0, y0, a: a * np.exp(-((x - x0) ** 2 + (y - y0) ** 2) / 4)
    g = bump(4, 5, 1.0) + bump(14, 5, 0.5) + bump(19, 0, 0.01)      # the last under the floor
    flat = g.copy()
    flat[5, 4] = flat[5, 5] = flat[5, 4]                            # a flat top: both kept
    i = _peaks(g.ravel(), 20, 10)
    assert [(k % 20, k // 20) for k in i] == [(4, 5), (14, 5)]
    assert {k % 20 for k in _peaks(flat.ravel(), 20, 10)} >= {4, 5}
    assert len(_peaks(np.zeros(200), 20, 10)) == 0


def test_the_field_maps_cover_the_board_and_peak_at_the_hotspots(solved, tmp_path):
    snap, setup, r = solved
    emi = r.emi
    for fmap, spots in ((emi.h_map, emi.h_hotspots), (emi.e_map, emi.e_hotspots)):
        x0, y0, x1, y1 = fmap.bounds
        assert x0 <= 0 and y0 <= 0 and x1 >= 32 and y1 >= 20          # the 32 x 20 board
        ny, nx = fmap.values.shape
        assert nx * ny <= 8000 and fmap.values.min() >= 0
        # Its strongest point is where the hotspot search found it (within a cell or so).
        j, i = np.unravel_index(np.argmax(fmap.values), fmap.values.shape)
        x, y = x0 + i * (x1 - x0) / (nx - 1), y0 + j * (y1 - y0) / (ny - 1)
        assert math.hypot(x - spots[0].x_mm, y - spots[0].y_mm) < 1.5
        assert fmap.values.max() == pytest.approx(spots[0].value, rel=0.25)
    assert r.fields.board_bounds == (0.0, 0.0, 32.0, 20.0)
    # The report draws them over the board, the hotspots numbered as in their tables.
    from kipi.noise.html_report import write_noise_report
    text = write_noise_report(tmp_path / "n.html", r, snap, setup).read_text(encoding="utf-8")
    emi_part = text[text.index("id='emi'"):text.index("id='notes'")]
    assert emi_part.count("data:image/png") == 3                    # H, E and the radiated
    assert "Click to sort'>#</th>" in emi_part and "<td>1</td>" in emi_part


def test_a_big_board_gets_a_zoomed_field_map_too(monkeypatch):
    from kipi.noise import html_report
    snap = buck_board(victim_gap=0.2)
    snap.outline = [Poly(rect(-40, -30, 90, 60))]
    setup = _setup(snap, [Victim("SIG")], shielded=False)
    r = solve_noise(snap, setup, "U1 buck")
    assert r.emi.h_map.bounds == (-40.0, -30.0, 90.0, 60.0)
    figs = []
    monkeypatch.setattr(html_report, "_png", lambda fig: figs.append(fig) or "x")
    html_report._field_map_png(r.emi.h_map, r.emi.h_hotspots, snap, 5.0)
    board, zoom = figs[0].axes[:2]
    assert board.get_xlim() == (-40.0, 90.0) and board.get_ylim() == (60.0, -30.0)   # y down
    assert zoom.get_xlim()[1] - zoom.get_xlim()[0] < 60
    assert not board.texts and len(zoom.texts) > 5                  # labels in the zoom only


def test_the_radiated_estimate_is_otts_small_loop():
    """One loop in free space carrying 1 mA at 100 MHz: E = 1.316e-14·f²·I·A/r."""
    corners = [[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]]
    segs = peec.polyline(corners, [0.2] * 4, 0.035)
    c = np.array([0.0, 0.0005 + 0j])                       # harmonic 2 of 50 MHz: 1 mA peak
    src = HSource("L", "L", 1, np.array([x.a for x in segs]), np.array([x.b for x in segs]),
                  np.ones(4), np.array([x.radius for x in segs]), None, 0.035, 10.0,
                  Spectrum("I", "A", 0.0, c))
    fs = FieldSources([src], [], 50e6, 0.035, 0.0)
    rep = near_field_report(fs, np.array([50e6, 100e6]))
    e = 1.316e-14 * (100e6) ** 2 * 1e-3 * 1e-4 / 3.0
    assert rep.worst_f_hz == 100e6
    assert rep.worst_dbuv == pytest.approx(20 * math.log10(e / 1e-6), abs=1e-6)
    assert rep.worst_limit_dbuv == 40.0 and rep.worst_source == "L"
    assert rep.h_hotspots[0].f_hz == 100e6 and not rep.e_hotspots


# ------------------------------------------------------------------ plane splits
def _slotted(victim_gap=0.2, slot=((17.0, 8.0), (19.0, 19.0))):
    """The buck with a slot cut through its ground plane across the victim's path."""
    snap = buck_board(victim_gap=victim_gap)
    (x0, y0), (x1, y1) = slot
    outer = rect(0, 0, 32, 20)
    hole = rect(x0, y0, x1, y1)
    snap.zones[0].filled = {IN1: [Poly(outer, [hole])]}
    snap.invalidate_index()
    return snap


def test_johnsons_slot_inductance():
    assert slot_inductance(10.0, 0.2) == pytest.approx(0.197e-9 * 10 * math.log(50), rel=1e-9)
    assert slot_inductance(0.2, 0.2) == 0.0 and slot_inductance(0.1, 0.2) == 0.0


def test_a_victim_crossing_a_slot_gets_the_detours_inductance():
    snap = _slotted()
    segs = [Segment((x, 14.3), (x + 0.5, 14.3), F, 0.2) for x in np.arange(11.5, 24.0, 0.5)]
    crossings = plane_splits(snap, segs, ["SIG", "SW"])
    assert len(crossings) == 1
    c = crossings[0]
    assert c.x_mm == pytest.approx(17.0) and c.y_mm == pytest.approx(14.3)
    assert c.from_ref == "GND (In1.Cu)" and c.to_ref == "no plane" and c.layer == "F.Cu"
    # The slot is 2 x 11 mm: the return goes round its nearer end, 5.5 mm away.
    assert c.detour_mm == pytest.approx(5.5)
    assert c.l_h == pytest.approx(slot_inductance(5.5, 0.2))
    assert segs[c.segment].a[0] == pytest.approx(17.0) and segs[c.segment].series_l_h == c.l_h
    assert sum(s.series_l_h for s in segs) == pytest.approx(c.l_h)
    assert "detours 5.5 mm" in c.describe()
    # A trace that stays over the plane crosses nothing.
    assert plane_splits(snap, [Segment((2, 2), (6, 2), F, 0.2), Segment((6, 2), (10, 2), F, 0.2)],
                        ["SIG"]) == []


def test_the_series_inductance_raises_the_lines_impedance():
    v = 299.792458e9 / math.sqrt(3.3)
    segs = [Segment((k * 1.0, 0.0), ((k + 1) * 1.0, 0.0), F, 0.2, 1 / (50 * v), 50 / v)
            for k in range(6)]
    f = np.array([200e6])
    terms = [Termination(segs[0].a, 50.0), Termination(segs[-1].b, 1e6, 1e-12)]
    emf = np.zeros((1, 6), complex)
    emf[0, 1] = 1.0
    plain = VictimNetwork(segs, {F: 0.035}).solve_many(f, terms, [5], emfs={"e": emf})["e"][0, 0]
    segs[3].series_l_h = 5e-9
    with_l = VictimNetwork(segs, {F: 0.035}).solve_many(f, terms, [5], emfs={"e": emf})["e"][0, 0]
    assert abs(with_l) != pytest.approx(abs(plain), rel=1e-3)


def test_a_solve_reports_the_crossing_and_couples_the_real_return(solved):
    snap = _slotted()
    setup = _setup(snap, [Victim("SIG")], board_model=True)
    r = solve_noise(snap, setup, "U1 buck")
    v = r.victims[0]
    assert len(v.splits) == 1 and v.splits[0].to_ref == "no plane"
    assert any("Crosses from GND" in w for w in v.warnings)
    assert any("detours" in line for line in r.summary_lines())
    # Over the slot the loops' direct field is not mirrored: the EMF there is larger than
    # with the plane whole.
    _, _, whole = solved
    assert v.m_h["C2 loop (phase 1)"] != pytest.approx(whole.victims[0].m_h["C2 loop (phase 1)"],
                                                       rel=1e-3)


def test_a_victim_leaving_one_plane_for_another_detours_through_a_capacitor():
    snap = buck_board(victim_gap=0.2)
    # VOUT copper under the victim's end on In1 beside the ground plane.
    snap.zones[0].filled = {IN1: [Poly(rect(0, 0, 20, 20))]}
    from kipi.core.model.board import Zone
    snap.zones.append(Zone("zvout", "VOUT", "", 0, filled={IN1: [Poly(rect(20, 0, 32, 20))]}))
    snap.invalidate_index()
    segs = [Segment((x, 14.3), (x + 0.5, 14.3), F, 0.2) for x in np.arange(11.5, 24.0, 0.5)]
    (c,) = plane_splits(snap, segs, ["SIG", "SW"], buck_setup())
    assert c.from_ref == "GND (In1.Cu)" and c.to_ref == "VOUT (In1.Cu)"
    assert "through C2" in c.note                       # the VOUT-GND capacitor at x 24
    d = math.hypot(24.0 - 20.0, 13.5 - 14.3)
    assert c.detour_mm == pytest.approx(d) and c.l_h == pytest.approx(slot_inductance(d, 0.2) + 0.5e-9)


def test_only_the_aggressors_planes_count():
    from kipi.core.model.board import Zone
    from kipi.noise.extract.common import aggressor_nets
    from kipi.noise.model import Aggressor
    setup = buck_setup()
    agg = Aggressor("U1 buck", vrm="U1")
    nets = aggressor_nets(setup, agg)
    assert {"GND", "VOUT", "VIN"} <= nets and "P3V3" not in nets
    segs = lambda: [Segment((x, 14.3), (x + 0.5, 14.3), F, 0.2)          # noqa: E731
                    for x in np.arange(11.5, 24.0, 0.5)]
    # Ground under the start, another rail's plane under the end: not the aggressor's.
    snap = buck_board(victim_gap=0.2)
    snap.zones[0].filled = {IN1: [Poly(rect(0, 0, 20, 20))]}
    snap.zones.append(Zone("z3v3", "P3V3", "", 0, filled={IN1: [Poly(rect(20, 0, 32, 20))]}))
    snap.invalidate_index()
    assert len(plane_splits(snap, segs(), ["SIG", "SW"], setup)) == 1
    s = segs()
    assert plane_splits(snap, s, ["SIG", "SW"], setup, nets) == []
    assert not any(x.series_l_h for x in s)
    # A slot in the aggressor's ground still counts.
    assert len(plane_splits(_slotted(), segs(), ["SIG", "SW"], setup, nets)) == 1


# ------------------------------------------------------------------ the GUI
@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_aggressor_pane_edits_the_winding(app):
    from qtpy.QtCore import Qt
    from kipi.noise.gui.panel import NoisePanel
    snap = buck_board()
    p = NoisePanel()
    p.set_board(snap, _setup(snap))
    ed = p.agg_list.editor
    p.agg_list.select_name("U1 buck")
    assert ed.ind_shielded.isChecked() and ed.ind_axis.currentData() == "vertical"
    ed.ind_shielded.setChecked(False)
    ed.ind_axis.setCurrentIndex(ed.ind_axis.findData("across"))
    ph = p.setup.aggressors[0].phases[0]
    assert (ph.inductor_shielded, ph.inductor_axis) == (False, "across")
    assert "{" not in ed.ind_shielded.toolTip() and "inductor_field" in ed.ind_axis.toolTip()
    assert ed.ind_shielded.checkState() == Qt.Unchecked
    p.close()


def _settled(app, p):
    """The field the panel works out off the UI thread, once it is done."""
    while p._field_task is not None:
        p.wait_fields(60000)
        app.processEvents()
    return p


def test_the_panel_offers_the_fields(app, solved):
    from qtpy.QtCore import Qt
    from kipi.noise.gui.panel import NoisePanel
    snap, setup, r = solved
    p = NoisePanel()
    p.set_board(snap, setup)
    seen = []
    p.coupling_changed.connect(lambda: seen.append(p.fields_overlay()))
    p.show_result(r)
    assert not p.fields_row.isHidden() and p.lst_fields.isHidden() and p.fields_overlay() is None
    p.cmb_field.setCurrentIndex(p.cmb_field.findData("H"))
    assert not p.lst_fields.isHidden()
    assert [p.lst_fields.item(i).text() for i in range(p.lst_fields.count())] == [
        "C1 loop", "C2 loop", "L1 field"]
    assert all(p.lst_fields.item(i).checkState() == Qt.Checked for i in range(3))
    assert p._field_task is not None and "Working out" in p.lbl_fields.text()
    _settled(app, p)
    assert seen[-1] is not None and seen[-1].unit == "A/m"
    assert "C1 loop, C2 loop, L1 field" in p.lbl_fields.text()
    # A change while one is under way: the latest is the one shown, worked out once.
    p.lst_fields.item(1).setCheckState(Qt.Unchecked)
    p.chk_animate.setChecked(True)
    _settled(app, p)
    assert "C2" not in seen[-1].note and seen[-1].frames is not None
    n = len(seen)
    p.chk_animate.setChecked(True)                          # nothing new asked for
    p.coupling_changed.emit()                               # nor worked out again
    assert len(seen) == n + 1 and seen[-1] is seen[-2]
    p.chk_animate.setChecked(False)
    _settled(app, p)
    # A clicked net adds its field, switching to E; a clicked part its loop, back to H.
    assert p.show_field_of("SW") and p.cmb_field.currentData() == "E"
    assert _settled(app, p).fields_overlay().unit == "V/m"
    assert p.show_field_of("C2") and p.cmb_field.currentData() == "H"
    assert p._ticked(p.lst_fields) == ["C2 loop (phase 1)"]
    assert p.show_field_of("L1") and p._ticked(p.lst_fields) == ["C2 loop (phase 1)",
                                                                  "L1 field (phase 1)"]
    assert not p.show_field_of("U9")
    p.show_result(r)                                        # the choices stay
    assert p.cmb_field.currentData() == "H" and p._ticked(p.lst_fields) == [
        "C2 loop (phase 1)", "L1 field (phase 1)"]
    _settled(app, p)
    p.clear_results()
    assert seen[-1] is None and p.fields_row.isHidden() and not p.show_field_of("C1")
    assert p.fields_overlay() is None

def test_the_window_draws_the_field_in_noise_mode(app, no_3d, solved):
    from qtpy.QtWidgets import QRadioButton
    from kipi.gui.main_window import MainWindow
    snap, setup, r = solved
    w = MainWindow()
    try:
        w.noise_panel.set_board(snap, setup)
        w.findChild(QRadioButton, "mode_noise").setChecked(True)
        w.noise_panel.show_result(r)
        p = w.noise_panel
        assert w.viewer.fields is None
        p.cmb_field.setCurrentIndex(p.cmb_field.findData("E"))
        assert w.viewer.fields is None                     # until it is worked out
        _settled(app, p)
        assert w.viewer.fields is not None and w.viewer.fields.unit == "V/m"
        assert w.views.currentWidget() is w.viewer
        w.findChild(QRadioButton, "mode_ac").setChecked(True)
        assert w.viewer.fields is None
        w.findChild(QRadioButton, "mode_noise").setChecked(True)
        assert w.viewer.fields is not None
        p.cmb_field.setCurrentIndex(p.cmb_field.findData("off"))
        assert w.viewer.fields is None
    finally:
        w.close()
