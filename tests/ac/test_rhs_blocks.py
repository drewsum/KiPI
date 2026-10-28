"""The AC field solve's right-hand sides in blocks of any width give the same impedances,
and one solve alone takes blocks as wide as the memory free allows (#215)."""

import numpy as np

from kipi.ac.solver.fem import ACSystem, PortDef
from kipi.core.solver import direct
from kipi.dc.pi.model import ACSettings, SolveSettings
from synthetic import make_board


def system():
    ac = ACSettings()
    ac.max_edge_mm = 1.0
    ports = [PortDef("P", "probe", ["p1"], ["p3"]), PortDef("Q", "probe", ["p2"], ["p3"])]
    return ACSystem(make_board(), ["+3V3"], ["GND"], ports, [], SolveSettings(), ac)


def test_block_width_leaves_the_impedances_alone(monkeypatch):
    s = system()
    w = 2 * np.pi * 1e6
    monkeypatch.setattr(direct, "rhs_block", lambda n, max_cols=64, memory=False: 1)
    one = s.port_impedance(w)
    monkeypatch.undo()
    widths = []
    real = direct.rhs_block

    def spy(n, max_cols=64, memory=False):
        widths.append((max_cols, memory))
        return real(n, max_cols, memory)
    monkeypatch.setattr(direct, "rhs_block", spy)
    wide = s.port_impedance(w)
    assert np.allclose(one, wide, rtol=1e-10, atol=0)
    # As many columns as there are, and sized by memory: this is a solve on its own.
    assert widths and all(m for _, m in widths)
    assert widths[0][0] == len(s.ports) + s.n_conductors


def test_a_threads_solve_keeps_the_fixed_budget(monkeypatch):
    s = system()
    seen = []
    real = direct.rhs_block

    def spy(n, max_cols=64, memory=False):
        seen.append(memory)
        return real(n, max_cols, memory)
    monkeypatch.setattr(direct, "rhs_block", spy)
    series = direct.ComplexSeries(native=False)
    s.port_impedance(2 * np.pi * 1e6, series=series)
    series.close()
    assert seen and not any(seen)
