"""Where a victim's noise couples in, in the 3D view (#152): each segment's share of the
receiver's peak, the overlay built from it, and its drawing."""

import os

import numpy as np
import pytest

from kipi.ac.model import CapModel
from kipi.noise.checks import seed_phases
from kipi.noise.model import Aggressor, Victim
from kipi.noise.network import Termination, VictimNetwork
from kipi.noise.report import load_results, save_results
from kipi.noise.solver import solve_noise
from kipi.noise.viz import coupling_overlay
from noise_boards import buck_board, buck_setup
from test_capacitive import _line


def _setup(snap, victims):
    setup = buck_setup()
    vrm = next(v for v in setup.vrms if v.name == "U1")
    a = Aggressor("U1 buck", "U1", fsw_hz=1e6)
    a.phases = seed_phases(snap, setup, vrm, "buck")
    a.phases[0].coss_f = 300e-12
    setup.aggressors.append(a)
    setup.caps = [CapModel("10u", 10e-6, 0.4e-9, 0.003, ["C1"]),
                  CapModel("47u", 47e-6, 0.6e-9, 0.002, ["C2"])]
    setup.victims = victims
    return setup


@pytest.fixture(scope="module")
def solved():
    snap = buck_board(victim_gap=0.2)
    setup = _setup(snap, [Victim("SIG")])
    return snap, setup, solve_noise(snap, setup, "U1 buck")


def test_segment_shares_match_a_solve_per_segment():
    """By reciprocity: each segment's share is the receiver's voltage with that segment's
    sources alone."""
    segs = _line(n=5, length=10.0)
    net = VictimNetwork(segs, {segs[0].layer: 0.035})
    f = np.array([1e6, 30e6, 200e6])
    terms = [Termination(segs[0].a, 33.0), Termination(segs[-1].b, 1e6, 3e-12)]
    rcv = net.nearest(segs[-1].b)
    rng = np.random.default_rng(1)
    cur = rng.normal(size=(3, 5)) + 1j * rng.normal(size=(3, 5))
    emf = rng.normal(size=(3, 5)) + 1j * rng.normal(size=(3, 5))
    w = np.exp(1j * rng.uniform(0, 6, 3))
    net.solve_many(f, terms, [rcv], {}, {}, transfer_to=rcv)
    shares = net.shares(w, currents=cur, emfs=emf)
    for s in range(5):
        node = np.zeros((3, net.n), complex)
        np.add.at(node.T, net.ends[s, 0], 0.5 * cur[:, s])
        np.add.at(node.T, net.ends[s, 1], 0.5 * cur[:, s])
        one = np.zeros_like(emf)
        one[:, s] = emf[:, s]
        out = net.solve_many(f, terms, [rcv], {"c": node}, {"e": one})
        v = out["c"][:, 0] + out["e"][:, 0]
        assert shares[s] == pytest.approx(2 * np.real(w @ v), rel=1e-9, abs=1e-15)


def test_the_shares_add_up_to_the_peak(solved, tmp_path):
    snap, setup, r = solved
    v = r.victims[0]
    total = sum(a.sum() for a in v.segment_v.values()) + v.common_v
    assert total == pytest.approx(v.peak_v, rel=1e-9)
    assert set(v.segment_v) == {"capacitive", "inductive"}
    # The capacitive share is where the trace runs beside the switch node.
    assert v.segment_v["capacitive"].sum() > 0
    assert v.segments_xyz.shape == (len(v.segment_v["capacitive"]), 2, 3)
    assert r.loop_paths and all(p.shape[1:] == (2, 3) for p in r.loop_paths.values())
    back = load_results(save_results(tmp_path / "c.kipi-noise-results", {r.aggressor: r},
                                     "b", setup))["results"]["U1 buck"]
    assert np.allclose(back.victims[0].segment_v["inductive"], v.segment_v["inductive"])
    assert set(back.loop_paths) == set(r.loop_paths)


def test_the_overlay_shows_one_mechanism_or_all(solved):
    _, _, r = solved
    v = r.victims[0]
    assert coupling_overlay(r, "SIG", "off") is None
    assert coupling_overlay(r, "nope", "total") is None
    cap = coupling_overlay(r, "SIG", "capacitive")
    assert np.allclose(cap.segment_mv, 1e3 * v.segment_v["capacitive"])
    assert len(cap.points) == 0
    assert cap.limit_mv == pytest.approx(np.abs(cap.segment_mv).max())
    ind = coupling_overlay(r, "SIG", "inductive")
    tot = coupling_overlay(r, "SIG", "total")
    assert np.allclose(tot.segment_mv, cap.segment_mv + ind.segment_mv)
    assert "peak" in tot.note
    common = coupling_overlay(r, "SIG", "common")
    if v.common_v:
        assert common.point_mv[0] == pytest.approx(1e3 * v.common_v)
    else:
        assert len(common.points) == 0 and "board model" in common.note


def test_the_hot_loops_overlay_draws_each_loop_in_its_colour(solved):
    """#178: the loops are their own view, one colour and name per capacitor, with
    arrows along the current, and are no longer part of the coupling views."""
    from kipi.noise.viz import LOOP_COLORS, LOOPS_ALL, hot_loops_overlay, loop_choices
    _, _, r = solved
    assert hot_loops_overlay(r, "off") is None and hot_loops_overlay(None, LOOPS_ALL) is None
    assert hot_loops_overlay(r, "C9 (phase 1)") is None
    assert [k for k, _ in loop_choices(r)] == ["off", "all", *r.loop_paths]
    assert loop_choices(None) == []
    ov = hot_loops_overlay(r, LOOPS_ALL)
    assert [lp.key for lp in ov.loops] == list(r.loop_paths)
    assert [lp.label for lp in ov.loops] == ["C1", "C2"]
    assert [lp.color for lp in ov.loops] == list(LOOP_COLORS[:2])
    c1 = ov.loops[0]
    assert np.array_equal(c1.segments, r.loop_paths["C1 (phase 1)"])
    assert len(c1.arrow_xyz) >= 4 and c1.arrow_xyz.shape == c1.arrow_dir.shape
    assert np.allclose(np.linalg.norm(c1.arrow_dir, axis=1), 1.0)
    # Every arrow sits on one of the loop's segments, pointing its way.
    for p, d in zip(c1.arrow_xyz, c1.arrow_dir):
        a, b = c1.segments[:, 0], c1.segments[:, 1]
        u = b - a
        ln = np.linalg.norm(u, axis=1)
        t = np.einsum("ij,ij->i", p - a, u) / np.maximum(ln, 1e-12) ** 2
        on = np.linalg.norm(a + t[:, None] * u - p, axis=1) < 1e-6
        assert any(on & (t > -1e-9) & (t < 1 + 1e-9) & (u @ d / np.maximum(ln, 1e-12) > 0.999))
    # The name goes at the capacitor: where the loop comes back to its positive pad.
    assert np.allclose(c1.label_xyz[:2], (6.0, 10.0))
    assert "C1 (phase 1)" in ov.note and "nH" in ov.note
    one = hot_loops_overlay(r, "C2 (phase 1)")
    assert [lp.key for lp in one.loops] == ["C2 (phase 1)"] and one.loops[0].color == LOOP_COLORS[1]
    assert not hasattr(coupling_overlay(r, "SIG", "inductive"), "loops")


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_panel_picks_the_view_for_the_selected_victim(app, solved):
    from kipi.noise.gui.panel import NoisePanel
    snap, setup, r = solved
    p = NoisePanel()
    p.set_board(snap, setup)
    changed = []
    p.coupling_changed.connect(lambda: changed.append(p.coupling_overlay()))
    p.show_result(r)
    assert not p.coupling_row.isHidden() and changed[-1] is None    # off by default
    p.cmb_coupling.setCurrentIndex(p.cmb_coupling.findData("inductive"))
    assert changed[-1] is not None and len(changed[-1].segments)
    assert "inductive" in p.lbl_coupling.text()
    p.clear_results()
    assert changed[-1] is None and p.coupling_row.isHidden()
    p.close()


def test_the_panel_offers_the_hot_loops(app, solved):
    from kipi.noise.gui.panel import NoisePanel
    snap, setup, r = solved
    p = NoisePanel()
    p.set_board(snap, setup)
    loops = []
    p.coupling_changed.connect(lambda: loops.append(p.hot_loops_overlay()))
    p.show_result(r)
    assert not p.loops_row.isHidden() and loops[-1] is None          # off by default
    assert [p.cmb_loops.itemText(i) for i in range(p.cmb_loops.count())] == [
        "Off", "All capacitors", "C1 (phase 1)", "C2 (phase 1)"]
    assert p.lbl_loops.isHidden()
    p.cmb_loops.setCurrentIndex(p.cmb_loops.findData("C2 (phase 1)"))
    assert [lp.key for lp in loops[-1].loops] == ["C2 (phase 1)"]
    assert "C2 (phase 1)" in p.lbl_loops.text() and not p.lbl_loops.isHidden()
    p.show_result(r)                                                 # the choice stays
    assert p.cmb_loops.currentData() == "C2 (phase 1)"
    p.clear_results()
    assert loops[-1] is None and p.loops_row.isHidden()
    p.close()


def test_the_window_draws_it_in_noise_mode_only(app, no_3d, solved):
    from qtpy.QtWidgets import QRadioButton
    from kipi.gui.main_window import MainWindow
    snap, setup, r = solved
    w = MainWindow()
    try:
        w.noise_panel.set_board(snap, setup)
        w.findChild(QRadioButton, "mode_noise").setChecked(True)
        w.noise_panel.show_result(r)
        p = w.noise_panel
        p.cmb_coupling.setCurrentIndex(p.cmb_coupling.findData("capacitive"))
        assert w.viewer.coupling is not None and w.views.currentWidget() is w.viewer
        assert w.viewer.hot_loops is None
        p.cmb_loops.setCurrentIndex(p.cmb_loops.findData("all"))
        assert w.viewer.hot_loops is not None and len(w.viewer.hot_loops.loops) == 2
        w.findChild(QRadioButton, "mode_ac").setChecked(True)
        assert w.viewer.coupling is None and w.viewer.hot_loops is None
        w.findChild(QRadioButton, "mode_noise").setChecked(True)
        assert w.viewer.coupling is not None and w.viewer.hot_loops is not None
    finally:
        w.close()
