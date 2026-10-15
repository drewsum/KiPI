"""DC solves of the validation boards against hand calculations (#70).

Each board is solved from its checked-in snapshot and setup, the way the plugin opens
them, and every expected value comes from the board's dimensions and textbook formulas:
R = ρL/(wt) for strips, ρθ/(t·ln(r_o/r_i)) for an arc, 0.5587 squares per right-angle
corner, ρh/(π(d·t_p - t_p²)) for a via barrel, the crowding of current into a row of
vias, the method of images for two pads on a disc, and the power tree's own equations
(LDO: I_in = I_out + I_q; switcher: I_in = V_out·I_out/(η·V_in) + I_q).
"""

import math

import pytest

from validation_boards import boards, open_board
from kipi.dc.pi.tree import solve_tree
from kipi.dc.solver.setup import RHO_CU_OHM_MM as RHO

CORNER_SQUARES = 0.5587       # a square right-angle bend, by conformal mapping


def _solve(name):
    snap, setup = open_board(name)
    return snap, setup, solve_tree(snap, setup)


def _load(tree, name):
    return next(x for x in tree.loads if x.name == name)


def _vrm(tree, name):
    return next(v for v in tree.vrms if v.name == name)


def _drop(tree, name):
    """Supply drop per amp of a load on a 1 V source with no ground net: the copper's
    resistance."""
    x = _load(tree, name)
    return (1.0 - x.v_supply) / x.current


def _thickness(snap, name):
    return next(c.thickness_mm for c in snap.stackup.copper if c.name == name)


def _z_mid(snap, name):
    c = next(c for c in snap.stackup.copper if c.name == name)
    return c.z_bottom_mm + c.thickness_mm / 2


def _clean(tree):
    assert not [w for w in tree.warnings if "converge" in w], tree.warnings
    for g in tree.groups.values():
        assert g.converged
        assert g.result.fields.summary["energy_balance_error"] < 1e-6


# ---------------------------------------------------------------- skinny traces

@pytest.fixture(scope="module")
def traces():
    return _solve("skinny_traces")


def test_outer_layer_trace(traces):
    snap, _, tree = traces
    g = boards.Traces
    length = g.X1 - g.X0 - g.PAD                    # pad edge to pad edge
    r = RHO * length / (g.W_F * _thickness(snap, "F.Cu"))
    assert _drop(tree, "U1") == pytest.approx(r, rel=2e-3)
    _clean(tree)


def test_inner_layer_trace_uses_the_thinner_copper(traces):
    snap, _, tree = traces
    g = boards.Traces
    t = _thickness(snap, "In1.Cu")
    assert t == pytest.approx(0.0175)
    r = RHO * (g.X1 - g.X0 - g.PAD) / (g.W_IN * t)
    assert _drop(tree, "U2") == pytest.approx(r, rel=2e-3)


def test_arc_trace_matches_the_annular_sector(traces):
    snap, _, tree = traces
    g = boards.Traces
    r_o, r_i = g.ARC_R + g.W_ARC / 2, g.ARC_R - g.W_ARC / 2
    theta = math.pi - 2 * math.asin(g.PAD / 2 / g.ARC_R)    # between the pad edges
    r = RHO * theta / (_thickness(snap, "F.Cu") * math.log(r_o / r_i))
    assert _drop(tree, "U3") == pytest.approx(r, rel=3e-3)


# ---------------------------------------------------------------- right-angle routes

@pytest.fixture(scope="module")
def bends():
    return _solve("right_angle")


def test_l_route_counts_its_corner_as_0_559_squares(bends):
    snap, _, tree = bends
    g = boards.RightAngle
    straight = (g.L_A - g.P - g.W) / g.W + (g.L_B - g.P - g.W) / g.W
    squares = _drop(tree, "U1") / (RHO / _thickness(snap, "F.Cu"))
    assert squares == pytest.approx(straight + CORNER_SQUARES, rel=1e-3)
    # The corner on its own (the mesh converges on it from below, as h^(2/3)).
    assert squares - straight == pytest.approx(CORNER_SQUARES, abs=0.008)
    _clean(tree)


def test_z_route_has_two_corners(bends):
    snap, _, tree = bends
    g = boards.RightAngle
    straight = (g.Z_A - g.P - g.W) + (g.Z_B - 2 * g.W) + (g.Z_C - g.W - g.P)
    squares = straight / g.W + 2 * CORNER_SQUARES
    rs = RHO / _thickness(snap, "F.Cu")
    assert _drop(tree, "U2") / rs == pytest.approx(squares, rel=1e-3)


# ---------------------------------------------------------------- layer change

def test_layer_change_through_a_row_of_vias():
    snap, setup, tree = _solve("layer_change")
    g = boards.LayerChange
    rs_top = RHO / _thickness(snap, "F.Cu")
    rs_bot = RHO / _thickness(snap, "In2.Cu")
    tp = setup.settings.plating_mm
    # Strips: from each pad's edge to the via row's centre line.
    r_strips = rs_top * (g.X_VIA - g.P) / g.W + rs_bot * (g.L - g.P - g.X_VIA) / g.W
    # Barrels in parallel, from F.Cu to In2.Cu (copper centre to copper centre).
    h = _z_mid(snap, "F.Cu") - _z_mid(snap, "In2.Cu")
    r_barrel = RHO * h / (math.pi * (g.DRILL * tp - tp * tp)) / g.N
    # Crowding into a row of N holes of radius r, pitch p, on each layer.
    crowd = math.log(g.pitch() / (2 * math.pi * g.DRILL / 2)) / (2 * math.pi * g.N)
    r_crowd = (rs_top + rs_bot) * crowd
    expected = r_strips + r_barrel + r_crowd
    assert _drop(tree, "U1") == pytest.approx(expected, rel=5e-3)
    # The vias share the current equally: the row is symmetric across the strip.
    vias = next(iter(tree.groups.values())).result.fields.vias
    assert len(vias) == g.N
    for v in vias:
        assert v.current == pytest.approx(g.AMPS / g.N, rel=2e-2)
    _clean(tree)


# ---------------------------------------------------------------- planes

@pytest.fixture(scope="module")
def plane_board():
    return _solve("planes")


def test_two_pads_on_a_disc_plane(plane_board):
    snap, _, tree = plane_board
    g = boards.Planes
    rs = RHO / _thickness(snap, "In1.Cu")
    # Method of images: each pad has an image at R0²/D, outside the disc.
    expected = rs / math.pi * math.log(2 * g.D / g.A * (g.R0 ** 2 + g.D ** 2)
                                       / (g.R0 ** 2 - g.D ** 2))
    assert _drop(tree, "U1") == pytest.approx(expected, rel=5e-3)
    _clean(tree)


def test_planes_on_two_layers_share_by_thickness(plane_board):
    snap, _, tree = plane_board
    g = boards.Planes
    t = _thickness(snap, "F.Cu") + _thickness(snap, "B.Cu")
    assert _thickness(snap, "B.Cu") == pytest.approx(2 * _thickness(snap, "F.Cu"))
    expected = RHO * (g.L - 2 * g.P) / (g.W * t)
    assert _drop(tree, "U2") == pytest.approx(expected, rel=2e-3)


# ---------------------------------------------------------------- power chain

@pytest.fixture(scope="module")
def chain():
    return _solve("power_chain")


def _r(snap, gap):
    return RHO * gap / (boards.PowerChain.W * _thickness(snap, "F.Cu"))


def _gap(pins, a, b):
    """Copper between the 1 mm pads ``a`` and ``b``, pad edge to pad edge."""
    return abs(pins[b] - pins[a]) - 1.0


def _hand(snap):
    """The whole chain by hand: currents first (downstream to upstream), then voltages."""
    g = boards.PowerChain
    out = {"U2.i_in": g.I_U3 + g.IQ_LDO}
    i_out1 = g.I_U4 + out["U2.i_in"]
    # Switcher input: V = V_src - R·(P/(η·V) + I_q), a quadratic in V.
    r_vin = _r(snap, _gap(g.VIN, "J1.1", "U1.1"))
    p = g.V_SMPS * i_out1 / g.ETA
    a = g.V_SRC - r_vin * g.IQ_SMPS
    out["U1.v_in"] = (a + math.sqrt(a * a - 4 * r_vin * p)) / 2
    out["U1.i_in"] = p / out["U1.v_in"] + g.IQ_SMPS
    # +3V3 and +3V3_F, joined by the ferrite.
    out["U4.v"] = g.V_SMPS - i_out1 * _r(snap, _gap(g.V3V3, "U1.2", "U4.1"))
    out["U2.v_in"] = out["U4.v"] - out["U2.i_in"] * (
        _r(snap, _gap(g.V3V3, "U4.1", "FB1.1")) + g.R_FB
        + _r(snap, _gap(g.V3V3_F, "FB1.2", "U2.1")))
    # Ground: every return flows back to J1.2, the 0 V reference.
    gd = g.GND
    out["U1.gnd"] = out["U1.i_in"] * _r(snap, _gap(gd, "J1.2", "U1.3"))
    out["U4.gnd"] = out["U1.gnd"] + (g.I_U3 + g.IQ_LDO + g.I_U4) * _r(
        snap, _gap(gd, "U1.3", "U4.2"))
    out["U2.gnd"] = out["U4.gnd"] + (g.I_U3 + g.IQ_LDO) * _r(snap, _gap(gd, "U4.2", "U2.3"))
    out["U3.gnd"] = out["U2.gnd"] + g.I_U3 * _r(snap, _gap(gd, "U2.3", "U3.2"))
    # +1V8: the LDO holds U3's supply 1.8 V above U3's ground (remote and ground sense).
    out["U3.v"] = g.V_LDO + out["U3.gnd"]
    out["U2.v_out"] = out["U3.v"] + g.I_U3 * _r(snap, _gap(g.V1V8, "U2.2", "U3.1"))
    return out


def test_power_chain_currents(chain):
    snap, _, tree = chain
    g, hand = boards.PowerChain, _hand(snap)
    assert _vrm(tree, "U2").i_in == pytest.approx(hand["U2.i_in"], rel=1e-9)
    assert _vrm(tree, "U1").i_out == pytest.approx(g.I_U4 + hand["U2.i_in"], rel=1e-9)
    assert _vrm(tree, "U1").i_in == pytest.approx(hand["U1.i_in"], rel=1e-6)
    assert _vrm(tree, "J1").i_out == pytest.approx(hand["U1.i_in"], rel=1e-6)
    (fb1,) = [s for gr in tree.groups.values() for s in gr.result.fields.series]
    assert fb1.current == pytest.approx(hand["U2.i_in"], rel=1e-6)
    assert fb1.power == pytest.approx(hand["U2.i_in"] ** 2 * g.R_FB, rel=1e-6)
    _clean(tree)


def test_power_chain_voltages(chain):
    snap, _, tree = chain
    g, hand = boards.PowerChain, _hand(snap)
    tol = dict(abs=2e-5)                                # 20 µV
    assert _vrm(tree, "U1").v_in == pytest.approx(hand["U1.v_in"], **tol)
    assert _vrm(tree, "U2").v_in == pytest.approx(hand["U2.v_in"], **tol)
    assert _vrm(tree, "U2").v_out == pytest.approx(hand["U2.v_out"], **tol)
    u3, u4 = _load(tree, "U3"), _load(tree, "U4")
    assert u4.v_supply == pytest.approx(hand["U4.v"], **tol)
    assert u4.v_ground == pytest.approx(hand["U4.gnd"], **tol)
    assert u3.v_ground == pytest.approx(hand["U3.gnd"], **tol)
    assert u3.v_supply == pytest.approx(hand["U3.v"], **tol)
    assert u3.v_effective == pytest.approx(g.V_LDO, abs=1e-8)      # sensed at the load
    assert u3.passed and u4.passed
