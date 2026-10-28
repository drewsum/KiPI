"""The AC field solve's right-hand sides in blocks of any width give the same impedances,
and one solve alone takes blocks as wide as the memory free allows (#215)."""

import numpy as np
import pytest

from kipi.ac.solver.fem import ACSystem, PortDef
from kipi.core.progress import ProgressReporter
from kipi.core.solver import direct
from kipi.dc.pi.model import ACSettings, SolveSettings
from synthetic import make_board
from tests.core.test_direct import _ExactNative


def system(more=0):
    ac = ACSettings()
    ac.max_edge_mm = 1.0
    ports = [PortDef("P", "probe", ["p1"], ["p3"]), PortDef("Q", "probe", ["p2"], ["p3"])]
    ports += [PortDef(f"R{i}", "probe", ["p2" if i % 2 else "p1"], ["p3"]) for i in range(more)]
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


@pytest.mark.parametrize("block", [None, 1])
def test_the_schur_complement_gives_the_same_impedances(monkeypatch, block):
    # #225: with more ports than twice the floating conductors, PARDISO's Schur complement
    # on the port terminals stands in for a solve per port; the floating conductors are
    # still solved in full, in blocks as narrow as memory needs.
    w = 2 * np.pi * 1e6
    z0 = system(more=6).port_impedance(w)
    s = system(more=6)
    assert s.n_conductors and len(s.ports) > 2 * s.n_conductors
    native = _ExactNative()
    monkeypatch.setattr(s._factors, "_native_solver", lambda: native)
    if block:
        monkeypatch.setattr(direct, "rhs_block", lambda n, max_cols=64, memory=False: block)
    z = s.port_impedance(w)
    assert native.calls and native.calls[0] is not None
    assert "Schur complement" in s.stats.method
    assert np.allclose(z, z0, rtol=1e-8, atol=1e-12)


def test_a_failed_schur_complement_solves_each_port(monkeypatch):
    w = 2 * np.pi * 1e6
    z0 = system(more=6).port_impedance(w)
    s = system(more=6)
    native = _ExactNative(schur_fails=True)
    monkeypatch.setattr(s._factors, "_native_solver", lambda: native)
    warned = []

    class Warn(ProgressReporter):
        def warn(self, text):
            warned.append(text)
    z = s.port_impedance(w, Warn())
    assert native.calls[0] is not None and native.calls[1:] == [None]
    assert "Schur" not in s.stats.method and warned
    assert np.allclose(z, z0, rtol=1e-8, atol=1e-12)


def test_few_ports_are_solved_one_by_one(monkeypatch):
    s = system()                            # 2 ports, 2 floating conductors
    native = _ExactNative()
    monkeypatch.setattr(s._factors, "_native_solver", lambda: native)
    s.port_impedance(2 * np.pi * 1e6)
    assert native.calls == [None]
