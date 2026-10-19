"""Electro-thermal: resistivity vs temperature and the board heat model."""


import numpy as np
import pytest
from shapely.geometry import box

from kipi.core.model.board import (
    BoardSnapshot, CopperLayer, DielectricLayer, Net, Poly, Stackup, Zone,
)
from kipi.dc.solver import CurrentLoad, NetProblem, SolveSettings, VoltageSource, run
from kipi.dc.thermal.cosim import solve_electrothermal
from kipi.dc.thermal.model import BoardThermalModel, ThermalSettings
from tree_boards import F, B, board, setup, by_name, R

TCR = 0.00393


def test_hot_copper_is_more_resistive():
    """A uniformly 120 degC strip drops 1 + 0.00393 * 100 times more than at 20 degC."""
    snap = board()
    prob = lambda temp: NetProblem("VIN", [VoltageSource("J1", ["j1_1"], 12.0)],
                                   [CurrentLoad("U1", ["u1_1"], 1.0)], SolveSettings(max_edge_mm=0.3),
                                   temperature=temp)
    cold = run(snap, prob(None))
    hot = run(snap, prob(lambda xy: np.full(len(xy), 120.0)))
    drop = lambda r: 12.0 - r.fields.terminals[1].v_mean
    assert drop(cold) == pytest.approx(R(8), rel=1e-9)
    assert drop(hot) == pytest.approx(R(8) * (1 + TCR * 100), rel=1e-9)
    # Current density is the same (same current, same copper); loss scales with rho.
    assert hot.fields.summary["power_loss"] == pytest.approx(
        cold.fields.summary["power_loss"] * (1 + TCR * 100), rel=1e-9)


def plate(width=40.0, cu_t=0.035):
    """A bare square board fully covered by one copper layer (for the heat model)."""
    copper = [CopperLayer(F, "F.Cu", 0, cu_t, 1.5 + cu_t), CopperLayer(B, "B.Cu", 1, cu_t, 0.0)]
    st = Stackup(copper, [DielectricLayer(1.5, "FR4", 4.5, F, B)], 1.5 + 2 * cu_t)
    sq = box(0, 0, width, width)
    zone = Zone("z", "N", "", 0, {F: [Poly(list(sq.exterior.coords)[:-1])]})
    return BoardSnapshot("plate", "", st, [Net(1, "N")], [], [], [], [], [zone],
                         [Poly(list(sq.exterior.coords)[:-1])])


def test_uniform_heating_matches_convection_balance():
    """Spread heat uniformly over both layers: every cell sits at T_amb + q / (2 h)."""
    s = ThermalSettings(enabled=True, ambient_c=25.0, airflow_m_s=2.0, grid_mm=1.0)
    m = BoardThermalModel(plate(), s)
    heat = m.empty_heat()
    q = 1e-4                                         # W per mm^2 of board
    heat[:, m.inside] = q * m.d * m.d / 2
    t = m.solve(heat)
    assert t.shape == (2,) + m.inside.shape
    expected = 25.0 + q / (2 * s.h_w_mm2_k())
    assert np.nanmax(np.abs(t - expected)) < 1e-6
    assert s.h_w_mm2_k() == pytest.approx((10 + 3.8 * 2) * 1e-6)


def test_point_heat_spreads_and_total_balances():
    s = ThermalSettings(enabled=True, ambient_c=20.0, grid_mm=1.0)
    m = BoardThermalModel(plate(), s)
    heat = m.empty_heat()
    m.deposit(heat, np.array([[20.0, 20.0]]), np.array([0.5]), F)
    t = m.solve(heat)
    # Energy balance: all 0.5 W leaves by convection, from the top and bottom faces.
    out = np.nansum((t - 20.0) * m.g_conv[:, None, None])
    assert out == pytest.approx(0.5, rel=1e-9)
    t = t[0]
    # Hottest at the source, falling off with distance; copper spreads it widely.
    x, y = np.meshgrid(m.x, m.y)
    r = np.hypot(x - 20, y - 20)
    assert t[np.unravel_index(np.nanargmax(t), t.shape)] == pytest.approx(np.nanmax(t))
    assert np.nanmax(t[r < 2]) > np.nanmax(t[(r > 10) & (r < 12)]) > np.nanmax(t[r > 18]) > 20


def test_more_airflow_runs_cooler():
    temps = []
    for v in (0.0, 1.0, 3.0):
        m = BoardThermalModel(plate(), ThermalSettings(enabled=True, airflow_m_s=v, grid_mm=1.0))
        heat = m.empty_heat()
        m.deposit(heat, np.array([[20.0, 20.0]]), np.array([0.5]))
        temps.append(np.nanmax(m.solve(heat)))
    assert temps[0] > temps[1] > temps[2]


def test_electrothermal_cosim_heats_and_raises_drop():
    snap = board()
    s = setup("ldo", iq=0.01)
    s.loads[0].current = 5.0                       # enough loss to warm the strips
    cold = solve_electrothermal(snap, s, ThermalSettings(enabled=True, iterations=1, grid_mm=0.5))
    hot = solve_electrothermal(snap, s, ThermalSettings(enabled=True, iterations=4, grid_mm=0.5))
    assert hot.thermal.t_max > 25.0
    assert len(hot.thermal.history) == 4
    # Iteration 1 is at ambient; later iterations see warmer, more resistive copper.
    assert by_name(hot.loads, "U2").drop > by_name(cold.loads, "U2").drop
    dts = [h["dt_max"] for h in hot.thermal.history]
    assert dts[-1] < dts[0]                         # settling


def test_first_iteration_solves_copper_at_ambient():
    snap = board()
    s = setup("ldo")
    at_20 = solve_electrothermal(snap, s, ThermalSettings(enabled=True, iterations=1,
                                                          ambient_c=20.0, grid_mm=0.5))
    at_85 = solve_electrothermal(snap, s, ThermalSettings(enabled=True, iterations=1,
                                                          ambient_c=85.0, grid_mm=0.5))
    ratio = by_name(at_85.loads, "U2").drop / by_name(at_20.loads, "U2").drop
    assert ratio == pytest.approx(1 + TCR * 65.0, rel=1e-3)


def test_loads_heat_the_board_with_the_power_they_take():
    snap = board()
    s = setup("ldo")
    res = solve_electrothermal(snap, s, ThermalSettings(enabled=True, iterations=1, grid_mm=0.5,
                                                   load_power=True))
    ld = by_name(res.loads, "U2")
    th = res.thermal
    assert th.heat_by_kind["loads"] == pytest.approx(ld.v_effective * ld.current, rel=1e-9)
    assert sum(th.heat_by_kind.values()) == pytest.approx(th.heat_w, rel=1e-9)
    # The LDO's own loss is its input power minus its output power.
    u1 = by_name(res.vrms, "U1")
    assert th.heat_by_kind["VRM conversion"] == pytest.approx(u1.p_loss, rel=1e-9)


def test_load_heat_raises_the_board_temperature():
    snap = board()
    s = setup("ldo")
    hot = solve_electrothermal(snap, s, ThermalSettings(enabled=True, iterations=1, grid_mm=0.5,
                                                   load_power=True))
    cool = solve_electrothermal(snap, s, ThermalSettings(enabled=True, iterations=1, grid_mm=0.5,
                                                   load_power=False))
    assert hot.thermal.t_max > cool.thermal.t_max + 1.0


@pytest.mark.parametrize("name,k,assumed", [
    ("FR4", 0.8, False), ("FR4 core", 0.8, False), ("FR408-HR", 0.8, False), ("370HR", 0.8, False),
    ("2116 RC54%", 0.6, False), ("3313 RC57%", 0.6, False), ("Prepreg", 0.6, False),
    ("Polyimide", 0.30, False), ("Kapton", 0.12, False), ("Polyolefin", 0.25, False),
    ("PTFE", 0.25, False), ("Teflon", 0.25, False), ("RO4350B", 0.69, False),
    ("Rogers RO4003C", 0.71, False), ("RT/duroid 5880", 0.20, False), ("Ceramic", 24.0, False),
    ("Al", 205.0, False), ("Aluminum nitride", 170.0, False), ("unobtainium", 0.8, True),
    ("", 0.8, True),
])
def test_dielectric_lookup(name, k, assumed):
    from kipi.dc.thermal.materials import lookup
    d = lookup(name)
    assert d.k_w_mk == pytest.approx(k) and d.assumed == assumed


def test_laminate_conductance_follows_stackup_materials():
    from kipi.core.model.board import DielectricLayer
    from kipi.dc.thermal.materials import laminate_conductance
    st = plate().stackup
    g_fr4, rows = laminate_conductance(st)
    assert g_fr4 == pytest.approx(0.8e-3 * 1.5)
    st.dielectrics = [DielectricLayer(1.5, "Ceramic")]
    g_cer, _ = laminate_conductance(st)
    assert g_cer == pytest.approx(24e-3 * 1.5)
    st.dielectrics = [DielectricLayer(1.5, "Ceramic", k_w_mk=2.0)]    # set in the Stackup window
    g_set, rows = laminate_conductance(st)
    assert g_set == pytest.approx(2e-3 * 1.5) and rows[0][2].matched == "set in the Stackup window"
    # A ceramic board spreads heat far better: the hot spot is much cooler.
    peaks = []
    for mat in ("FR4", "Ceramic"):
        snap = plate()
        snap.stackup.dielectrics = [DielectricLayer(1.5, mat)]
        snap.zones = []                                   # bare laminate, no copper spreading
        m = BoardThermalModel(snap, ThermalSettings(enabled=True, grid_mm=1.0))
        heat = m.empty_heat()
        m.deposit(heat, np.array([[20.0, 20.0]]), np.array([0.5]))
        peaks.append(np.nanmax(m.solve(heat)))
    assert peaks[0] > 2 * peaks[1]


def test_every_listed_material_is_found_by_its_own_name():
    from kipi.dc.thermal.materials import catalog, lookup
    mats = catalog()
    assert mats[0].material == "FR4 core" and len({m.material for m in mats}) == len(mats)
    for m in mats:
        found = lookup(m.material)
        assert found.matched == m.material and not found.assumed
        assert (found.k_w_mk, found.er) == (m.k_w_mk, m.er)
    er = {m.material: m.er for m in mats}
    assert er["Rogers RO4350B"] == pytest.approx(3.48)
    assert er["aluminum (metal core)"] == 0.0           # a metal: Default εr applies


def test_a_layers_own_tcr_is_used():
    """A layer given another conductor heats up by its own TCR, not the setup's."""
    snap = board()
    layer = snap.stackup.layer(F).name
    prob = lambda temp: NetProblem(
        "VIN", [VoltageSource("J1", ["j1_1"], 12.0)], [CurrentLoad("U1", ["u1_1"], 1.0)],
        SolveSettings(max_edge_mm=0.3, tcr_by_layer={layer: 0.006}), temperature=temp)
    cold = run(snap, prob(None))
    hot = run(snap, prob(lambda xy: np.full(len(xy), 120.0)))
    drop = lambda r: 12.0 - r.fields.terminals[1].v_mean
    assert drop(hot) / drop(cold) == pytest.approx(1 + 0.006 * 100, rel=1e-9)


def test_a_layers_conductor_sets_its_heat_spreading():
    copper = BoardThermalModel(plate(), ThermalSettings(enabled=True, grid_mm=1.0))
    alu = BoardThermalModel(plate(), ThermalSettings(enabled=True, grid_mm=1.0),
                            conductor_by_layer={"F.Cu": "Aluminum"})
    lam = 0.8e-3 * 1.5 / 2                         # each layer carries half the laminate
    assert np.nanmax(copper.g[0]) - lam == pytest.approx(0.39 * 0.035, rel=1e-6)
    assert np.nanmax(alu.g[0]) - lam == pytest.approx(0.237 * 0.035, rel=1e-6)
    assert np.nanmax(alu.g[1]) == pytest.approx(lam)        # B.Cu: no copper


def test_dc_and_thermal_solves_use_the_direct_backend(monkeypatch):
    # #108: both the copper and the heat systems go through kipi.core.solver.direct, so
    # they get PARDISO wherever it is installed (SuperLU otherwise).
    from kipi.core.solver import direct
    names, factorize = [], direct.factorize

    def spy(A, *a, **k):
        f = factorize(A, *a, **k)
        names.append(f.name)
        return f

    monkeypatch.setattr(direct, "factorize", spy)
    snap = board()
    solve_electrothermal(snap, setup("ldo"), ThermalSettings(enabled=True, iterations=2,
                                                             grid_mm=0.5))
    assert len(names) >= 3                          # the heat grid, then copper per pass
    assert set(names) == {direct.backend()}


# ------------------------------------------------------------- per-layer temperatures

def test_heat_on_one_side_crosses_the_laminate():
    """Uniform heat on F.Cu only: the 1D balance of the two faces through 1.5 mm of FR4.
    Per mm²: q = h (T_F + T_B) in rises, and (T_F - T_B) / R = h T_B, R = t / k_through."""
    s = ThermalSettings(enabled=True, ambient_c=25.0, grid_mm=1.0)
    m = BoardThermalModel(plate(), s)
    heat = m.empty_heat()
    q = 1e-4
    heat[0, m.inside] = q * m.d * m.d
    t = m.solve(heat)
    h, r = s.h_w_mm2_k(), 1.5 / 0.3e-3                 # FR4 core: 0.3 W/m·K through
    t_top = q / (h * (1 + 1 / (1 + h * r)))
    t_bottom = t_top / (1 + h * r)
    assert np.nanmax(np.abs(t[0] - 25.0 - t_top)) < 1e-6
    assert np.nanmax(np.abs(t[1] - 25.0 - t_bottom)) < 1e-6
    assert t_top - t_bottom > 0.04 * t_top            # no longer one temperature


def test_a_one_layer_board_loses_heat_from_both_faces():
    snap = plate()
    snap.stackup = Stackup([snap.stackup.copper[0]], [DielectricLayer(1.5, "FR4", 4.5, F, -1)],
                           1.5 + 0.035)
    s = ThermalSettings(enabled=True, grid_mm=1.0)
    m = BoardThermalModel(snap, s)
    assert m.layers == [F] and len(m.g_conv) == 1
    heat = m.empty_heat()
    heat[:, m.inside] = 1e-4
    assert np.nanmax(np.abs(m.solve(heat) - 25.0 - 1e-4 / (2 * s.h_w_mm2_k()))) < 1e-6


def test_vias_carry_heat_between_layers():
    from kipi.core.model.board import Via
    from kipi.dc.solver.via_barrel import barrel_copper_area, default_via_rules, rule_for
    s = ThermalSettings(enabled=True, grid_mm=1.0)
    bare = BoardThermalModel(plate(), s)
    snap = plate()
    snap.vias = [Via(f"v{i}", "N", (19.5 + 0.5 * (i % 3), 19.5 + 0.5 * (i // 3)), 0.3, 0.6,
                     [F, B]) for i in range(9)]
    vias = BoardThermalModel(snap, s)
    i, j = np.searchsorted(vias.y, 20.0), np.searchsorted(vias.x, 20.0)
    area = barrel_copper_area(rule_for(default_via_rules(), "through", 0.3), 0.3, None, 1.7e-5)
    one = 0.39 * area / (1.5 + 0.035)
    assert vias.g_vertical.sum() - bare.g_vertical.sum() == pytest.approx(9 * one, rel=1e-9)
    # A part on F heating the stitched spot: the vias pull B.Cu up to it.
    spread = []
    for m in (bare, vias):
        heat = m.empty_heat()
        m.deposit(heat, np.array([[20.0, 20.0]]), np.array([0.5]), F)
        t = m.solve(heat)
        spread.append(t[0, i, j] - t[1, i, j])
    assert spread[1] < 0.5 * spread[0]


def test_heat_without_a_layer_is_shared():
    m = BoardThermalModel(plate(), ThermalSettings(enabled=True, grid_mm=1.0))
    heat = m.empty_heat()
    m.deposit(heat, np.array([[5.0, 5.0], [6.0, 6.0]]), np.array([1.0, 2.0]), [-1, B])
    assert heat[0].sum() == pytest.approx(0.5) and heat[1].sum() == pytest.approx(2.5)


def test_laminate_is_shared_by_the_copper_it_touches():
    from kipi.dc.thermal.materials import laminate_layers
    ids = [3, 4, 5, 34]
    t = [0.035] * 4
    diel = [DielectricLayer(0.2, "2116 prepreg", 4.2, 3, 4),
            DielectricLayer(1.0, "FR4 core", 4.5, 4, 5),
            DielectricLayer(0.2, "2116 prepreg", 4.2, 5, 34)]
    z, copper = 1.54, []
    for k, lid in enumerate(ids):
        z -= t[k] + (diel[k - 1].thickness_mm if k else 0)
        copper.append(CopperLayer(lid, f"L{k}", k, t[k], z))
    st = Stackup(copper, diel, 1.54)
    g, r = laminate_layers(st)
    assert g == pytest.approx([0.6e-3 * 0.1, 0.6e-3 * 0.1 + 0.8e-3 * 0.5,
                               0.8e-3 * 0.5 + 0.6e-3 * 0.1, 0.6e-3 * 0.1])
    assert r == pytest.approx([0.2 / 0.25e-3, 1.0 / 0.3e-3, 0.2 / 0.25e-3])
    st.dielectrics = []                     # no dielectric data: the gaps as FR4 core
    g, r = laminate_layers(st)
    assert r == pytest.approx([0.2 / 0.3e-3, 1.0 / 0.3e-3, 0.2 / 0.3e-3])
    # A k typed in the Stackup window is in-plane; through it keeps FR4's 0.3 / 0.8.
    st.dielectrics = [DielectricLayer(0.2, "FR4", 4.5, 3, 4, k_w_mk=1.6),
                      *diel[1:]]
    assert laminate_layers(st)[1][0] == pytest.approx(0.2 / 0.6e-3)


def test_each_layer_of_the_cosim_has_its_own_temperature():
    """The rail is on F.Cu and the ground return on B.Cu: F.Cu, carrying the regulator's and
    the rail's heat, runs hotter, and the solvers read each layer's own copper."""
    from kipi.dc.pi.report import layer_temps
    snap = board()
    s = setup("ldo", ground=True)
    tree = solve_electrothermal(snap, s, ThermalSettings(enabled=True, iterations=2,
                                                         grid_mm=0.5))
    th = tree.thermal
    assert th.layers == [F, B] and th.temps.shape[0] == 2
    rows = {r.layer: r for r in layer_temps(tree, snap)}
    assert rows["F.Cu"].t_max > rows["B.Cu"].t_max > 25.0
    x, y, t = th.hottest()
    assert t == pytest.approx(th.hottest(F)[2]) and t > th.hottest(B)[2]
    T = th.sampler()
    assert T.on_layers([[x, y]], F)[0] > T.on_layers([[x, y]], B)[0]
    assert T([[x, y]])[0] == pytest.approx(T.on_layers([[x, y]], F)[0])   # the hottest


def test_results_saved_before_layers_read_as_one_sheet():
    from kipi.dc.thermal.cosim import ThermalResult
    from kipi.dc.thermal.model import ThermalSettings as TS
    x, y = np.arange(3.0), np.arange(2.0)
    temps = np.array([[30.0, 31.0, 32.0], [33.0, 34.0, 35.0]])
    th = ThermalResult.__new__(ThermalResult)
    th.__setstate__({"settings": TS(), "x": x, "y": y, "temps": temps, "history": []})
    assert th.temps.shape == (1, 2, 3) and th.layers == []
    assert th.hottest() == (2.0, 1.0, 35.0) and th.hottest(F) == (2.0, 1.0, 35.0)
    T = th.sampler()
    assert T.on_layers([[1.0, 0.0]], F)[0] == pytest.approx(31.0) == T([[1.0, 0.0]])[0]


def test_the_solver_reads_each_layers_own_copper():
    """A temperature that knows layers: the strip on F.Cu sees F.Cu's, not the hottest."""
    class ByLayer:
        def __call__(self, xy):
            return np.full(len(xy), 220.0)

        def on_layers(self, xy, layers):
            return np.where(np.broadcast_to(layers, (len(xy),)) == F, 120.0, 220.0)

    snap = board()
    prob = lambda temp: NetProblem("VIN", [VoltageSource("J1", ["j1_1"], 12.0)],
                                   [CurrentLoad("U1", ["u1_1"], 1.0)], SolveSettings(max_edge_mm=0.3),
                                   temperature=temp)
    hot = run(snap, prob(ByLayer()))
    assert 12.0 - hot.fields.terminals[1].v_mean == pytest.approx(R(8) * (1 + TCR * 100), rel=1e-9)


def test_solder_mask_and_silkscreen_hold_heat_in():
    """Uniform heat on a coated board: each face loses q_face / (1/h + sum t/k) (#82)."""
    from kipi.core.model.board import MASK, SILK, Coating
    from kipi.dc.thermal.materials import MASK_K_W_MK, SILK_K_W_MK, coating_resistance
    s = ThermalSettings(enabled=True, ambient_c=25.0, grid_mm=1.0)
    snap = plate()
    bare = BoardThermalModel(snap, s)
    snap.stackup.coatings = [Coating(SILK, "top", 0.02), Coating(MASK, "top", 0.03),
                             Coating(MASK, "bottom", 0.03), Coating(SILK, "bottom", 0.02)]
    r = 0.02 / (SILK_K_W_MK * 1e-3) + 0.03 / (MASK_K_W_MK * 1e-3)
    assert coating_resistance(snap.stackup, "top") == pytest.approx(r)
    assert coating_resistance(snap.stackup, "bottom") == pytest.approx(r)
    m = BoardThermalModel(snap, s)
    h = s.h_w_mm2_k()
    q = 1e-4                                         # W per mm^2 of board
    heat = m.empty_heat()
    heat[:, m.inside] = q * m.d * m.d / 2
    t = m.solve(heat)
    expected = 25.0 + q / (2 / (1 / h + r))
    assert np.nanmax(np.abs(t - expected)) < 1e-6
    hot = np.nanmax(t) - 25.0
    cold = np.nanmax(bare.solve(heat)) - 25.0
    assert cold < hot < cold * 1.01                  # thin coatings: a little warmer
