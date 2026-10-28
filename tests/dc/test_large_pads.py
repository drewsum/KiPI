"""Large terminal pads solved as copper, their part connected at a contact inside them
(#207): against the radial spreading formula, and unchanged when the option is off."""

import math
import pickle

import numpy as np
import pytest
from shapely.geometry import Point

from kipi.core.model.board import Pad
from kipi.dc.pi.model import PISetup
from kipi.dc.solver import CurrentLoad, NetProblem, SolveSettings, VoltageSource, run
from kipi.dc.solver.setup import RHO_CU_OHM_MM as RHO
from tests.dc.test_solver import F, T_CU, pad, poly, two_layer

R_OUT, R_SRC = 10.0, 9.0


def _circle(r, x=0.0, y=0.0):
    return Point(x, y).buffer(r, quad_segs=64)


def _disc(load_pad, holes=()):
    """A disc fed at its rim (the source pad, an annulus) with ``load_pad`` at its centre."""
    disc = _circle(R_OUT)
    for h in holes:
        disc = disc.difference(h)
    src = pad("src", "J1", F, _circle(R_OUT).difference(_circle(R_SRC)))
    return two_layer([(F, disc)], [src, load_pad])


def _drop(snap, **settings):
    s = SolveSettings(max_edge_mm=0.4, circle_quad_segs=16, **settings)
    prob = NetProblem("N", [VoltageSource("VRM", ["src"], 1.0)],
                      [CurrentLoad("LOAD", ["ld"], 1.0)], s)
    res = run(snap, prob)
    return 1.0 - res.fields.terminals[1].v_mean, res


def _radial(r_in):
    return RHO / (2 * math.pi * T_CU) * math.log(R_SRC / r_in)


R_PAD = 3.0          # 28 mm² of copper


def test_off_keeps_the_pad_equipotential():
    drop, _ = _drop(_disc(pad("ld", "U1", F, _circle(R_PAD))))
    assert drop == pytest.approx(_radial(R_PAD), rel=2e-3)


def test_smd_pad_connects_at_its_central_share():
    snap = _disc(pad("ld", "U1", F, _circle(R_PAD)))
    drop, res = _drop(snap, large_pads_as_copper=True, large_pad_mm2=10.0,
                      pad_contact_share=0.25)
    # The contact is the pad scaled to a quarter of its area: half its radius.
    assert drop == pytest.approx(_radial(R_PAD / 2), rel=2e-3)
    # The copper of the pad around the contact carries current now.
    nodes = res.mesh.pad_nodes["ld"][F]
    r = np.hypot(*res.mesh.points[nodes].T)
    assert r.max() == pytest.approx(R_PAD / 2, abs=0.01)


def test_pad_below_the_threshold_stays_equipotential():
    snap = _disc(pad("ld", "U1", F, _circle(R_PAD)))
    drop, _ = _drop(snap, large_pads_as_copper=True, large_pad_mm2=30.0)
    assert drop == pytest.approx(_radial(R_PAD), rel=2e-3)


def test_whole_pad_contact_is_the_same_as_off():
    snap = _disc(pad("ld", "U1", F, _circle(R_PAD)))
    off, r_off = _drop(snap)
    on, r_on = _drop(snap, large_pads_as_copper=True, large_pad_mm2=10.0,
                     pad_contact_share=1.0)
    assert on == off
    assert np.array_equal(r_off.mesh.points, r_on.mesh.points)


def test_smaller_contact_drops_more():
    snap = _disc(pad("ld", "U1", F, _circle(R_PAD)))
    drops = [_drop(snap, large_pads_as_copper=True, large_pad_mm2=10.0,
                   pad_contact_share=f)[0] for f in (1.0, 0.5, 0.25, 0.1)]
    assert all(a < b for a, b in zip(drops, drops[1:]))


def test_plated_hole_pad_connects_at_its_wall():
    drill = 3.2
    hole = _circle(drill / 2)
    mh = Pad("ld", "N", "H1", "1", (0.0, 0.0), "pth", drill_mm=(drill, drill),
             shapes={F: [poly(_circle(R_PAD))]})
    snap = _disc(mh, holes=[hole])
    off, _ = _drop(snap)
    on, res = _drop(snap, large_pads_as_copper=True, large_pad_mm2=10.0)
    assert off == pytest.approx(_radial(R_PAD), rel=2e-3)
    assert on == pytest.approx(_radial(drill / 2), rel=2e-3)
    r = np.hypot(*res.mesh.points[res.mesh.pad_nodes["ld"][F]].T)
    assert r.max() == pytest.approx(drill / 2, abs=0.01)


def test_settings_round_trip_and_old_results_load(tmp_path):
    setup = PISetup()
    setup.settings.large_pads_as_copper = True
    setup.settings.large_pad_mm2 = 42.0
    setup.settings.pad_contact_share = 0.4
    path = setup.save(tmp_path / "b.kipi-setup.yml")
    s = PISetup.load(path).settings
    assert (s.large_pads_as_copper, s.large_pad_mm2, s.pad_contact_share) == (True, 42.0, 0.4)
    # Settings pickled before #207 have none of the fields.
    old = SolveSettings()
    state = {k: v for k, v in old.__dict__.items()
             if k not in ("large_pads_as_copper", "large_pad_mm2", "pad_contact_share")}
    restored = SolveSettings.__new__(SolveSettings)
    restored.__setstate__(state)
    restored = pickle.loads(pickle.dumps(restored))
    assert restored.pad_mesh_options() == {"large_pad_mm2": None, "pad_contact_share": 0.25}
