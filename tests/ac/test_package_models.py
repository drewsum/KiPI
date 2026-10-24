"""Package PI models (#53): Touchstone files read onto a board part, PCB ports joined to
the board at its pins, and die ports solved as probes with no place on the board."""

import os

import numpy as np
import pytest

from ac_boards import plane_pair, plane_setup
from kipi.ac.checks import validate_ac
from kipi.ac.model import DIE, OPEN, PCB, SHORT, PackageModel, PackagePort
from kipi.ac.package import (
    block_diag, connect, guess_ports, open_removed_dies, rename_package, sync_die_probes,
)
from kipi.ac.solver import solve_ac
from kipi.core.model import touchstone as ts
from kipi.dc.pi.model import PISetup

R, L, C, ESR = 0.05, 0.1e-9, 100e-9, 1e-3
FF = np.logspace(4, 9, 501)
SWEEP = dict(f_start_hz=1e5, f_stop_hz=1e8, points_per_decade=10, field_points_per_decade=2)


def _y_series_die(f):
    """A package that is R + jωL from the balls to the bumps, with an on-die capacitor."""
    w = 2 * np.pi * np.asarray(f)
    ys = 1 / (R + 1j * w * L)
    yc = 1 / (1 / (1j * w * C) + ESR)
    y = np.zeros((len(w), 2, 2), complex)
    y[:, 0, 0], y[:, 1, 1] = ys, ys + yc
    y[:, 0, 1] = y[:, 1, 0] = -ys
    return y


def _package(tmp_path, die=DIE):
    path = ts.write(tmp_path / "pkg.s2p", FF, ts.s_from_y(_y_series_die(FF)),
                    names=("+3V3_BGA", "+3V3_DIE"))
    return PackageModel("PKG", "U1", path.read_text(), 2, "pkg.s2p", [
        PackagePort(1, "+3V3_BGA", PCB, "+3V3", ["1"], "GND", ["2"]),
        PackagePort(2, "+3V3_DIE", die, "+3V3", [], "GND", [])])


# ---------------------------------------------------------------- Touchstone
def test_touchstone_1_formats_and_the_two_port_order(tmp_path):
    s = np.array([[0.1 + 0.2j, 0.3 - 0.1j], [0.5 + 0.0j, -0.2 + 0.4j]])
    ri = ts.parse("# MHz S RI R 50\n1 " + " ".join(
        f"{v.real} {v.imag}" for v in (s[0, 0], s[1, 0], s[0, 1], s[1, 1])) + "\n", 2)
    assert ri.freqs_hz[0] == 1e6
    assert np.allclose(ri.s[0], s)                            # 11 21 12 22
    ma = " ".join(f"{abs(v)} {np.degrees(np.angle(v))}" for v in s.T.ravel())
    assert np.allclose(ts.parse(f"! comment\n# GHz S MA\n2 {ma}\n", 2).s[0], s)
    db = " ".join(f"{20 * np.log10(abs(v))} {np.degrees(np.angle(v))}" for v in s.T.ravel())
    net = ts.parse(f"#hz s db r 50\n5 {db}\n", 2)
    assert np.allclose(net.s[0], s) and net.freqs_hz[0] == 5
    # Three ports and more: row by row, wrapped over lines.
    s3 = np.arange(9).reshape(3, 3) * 0.05 + 0.01j
    body = " ".join(f"{v.real} {v.imag}" for v in s3.ravel())
    words = body.split()
    text = "# Hz S RI\n1 " + " ".join(words[:8]) + "\n" + " ".join(words[8:]) + "\n"
    assert np.allclose(ts.parse(text, 3).s[0], s3)
    assert ts.read(ts.write(tmp_path / "a.s3p", [1.0, 2.0], np.stack([s3, s3]))).n_ports == 3


def test_touchstone_z_and_y_are_normalized_in_version_1_only():
    z = 0.25                                                  # ohms, one port
    s = (z - 50) / (z + 50)
    v1 = ts.parse(f"# Hz Z RI R 50\n1 {z / 50} 0\n", 1)
    v2 = ts.parse(f"[Version] 2.0\n# Hz Z RI R 50\n[Number of Ports] 1\n"
                  f"[Number of Frequencies] 1\n[Network Data]\n1 {z} 0\n[End]\n")
    assert np.isclose(v1.s[0, 0, 0], s) and np.isclose(v2.s[0, 0, 0], s)
    assert np.isclose(ts.parse("# Hz Y RI R 50\n1 200 0\n", 1).s[0, 0, 0], s)


def test_touchstone_2_keywords():
    s = np.array([[0.1, 0.2], [0.3, 0.4]])
    text = ("[Version] 2.0\n# Hz S RI R 50\n[Number of Ports] 2\n"
            "[Two-Port Data Order] 12_21\n[Number of Frequencies] 2\n"
            "[Reference] 50\n 25\n[Begin Information]\nanything\n[End Information]\n"
            "[Network Data]\n"
            "1 0.1 0 0.2 0 0.3 0 0.4 0\n2 0.1 0 0.2 0 0.3 0 0.4 0\n[End]\n")
    net = ts.parse(text)
    assert np.allclose(net.s[0], s) and list(net.z0) == [50, 25]
    lower = ("[Version] 2.0\n# Hz S RI\n[Number of Ports] 2\n[Two-Port Data Order] 12_21\n"
             "[Matrix Format] Lower\n[Network Data]\n1 0.1 0 0.3 0 0.4 0\n")
    assert np.allclose(ts.parse(lower).s[0], [[0.1, 0.3], [0.3, 0.4]])


def test_touchstone_refuses_what_it_cannot_use():
    for text, n, why in (
            ("# Hz H RI\n1 0 0 0 0 0 0 0 0\n", 2, "G and H"),
            ("# Hz S RI\n1 0 0 2\n", 1, "whole frequency"),
            ("# Hz S RI\n2 0 0\n1 0 0\n", 1, "must increase"),
            ("# Hz S RI\n", 1, "no network data"),
            ("# Hz S RI\n1 0 0\n", None, "number of ports"),
            ("# Hz S XX\n1 0 0\n", 1, "option line"),
            ("[Version] 2.0\n# Hz S RI\n[Number of Ports] 2\n[Network Data]\n"
             "1 0 0 0 0 0 0 0 0\n", None, "Two-Port Data Order"),
            ("[Version] 2.0\n# Hz S RI\n[Number of Ports] 1\n[Mixed-Mode Order] D1,2\n"
             "[Network Data]\n1 0 0\n", None, "mixed-mode")):
        with pytest.raises(ts.TouchstoneError, match=why):
            ts.parse(text, n)


def test_touchstone_port_names_and_noise_data():
    text = ("! Port[1] = VCC_BGA\n! Port 2: VCC_DIE\n# Hz S RI\n"
            "1 0 0 0 0 0 0 0 0\n2 0 0 0 0 0 0 0 0\n"
            "1 0 0 0 0\n")                                 # noise: the frequency drops
    net = ts.parse(text, 2)
    assert net.names == ["VCC_BGA", "VCC_DIE"] and len(net.freqs_hz) == 2
    assert ts.parse("# Hz S RI\n1 0 0\n", 1).names == ["Port 1"]
    assert ts.ports_from_name("x.S12P") == 12 and ts.ports_from_name("x.txt") is None


def test_interpolated_and_held_outside_the_file():
    y = _y_series_die(FF)
    net = ts.parse(_text(FF, y), 2)
    f = np.array([1e3, 3.3e6, 1e10])
    got = net.y_at(f)
    assert np.allclose(got[1], _y_series_die([3.3e6])[0], rtol=1e-3)
    assert np.allclose(got[0], y[0]) and np.allclose(got[2], y[-1])
    assert net.covers(1e5, 1e8) and not net.covers(1e3, 1e8)


def _text(f, y):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        return ts.write(Path(d) / "p.s2p", f, ts.s_from_y(y)).read_text()


# ---------------------------------------------------------------- the network maths
def test_connect_matches_a_hand_worked_network():
    """A board that is Zb at a probe on the same pins as the package balls."""
    f = np.array([1e6, 1e7])
    w = 2 * np.pi * f
    zb = 0.01 + 1j * w * 1e-10
    zr = np.repeat(zb[:, None, None], 2, axis=1).repeat(2, axis=2)    # probe and ball alike
    y = _y_series_die(f)
    z = connect(zr, 1, block_diag([y], [1]), 1)
    zs, zc = R + 1j * w * L, 1 / (1j * w * C) + ESR
    assert np.allclose(z[:, 1], 1 / (1 / zc + 1 / (zs + zb)))       # the die
    assert np.allclose(z[:, 0], 1 / (1 / zb + 1 / (zs + zc)))       # the board probe


def test_open_and_shorted_ports_are_reduced_out(tmp_path):
    k = _package(tmp_path, die=OPEN)
    from kipi.ac.package import PackageUse
    f = np.array([1e6])
    w = 2 * np.pi * f
    opened = PackageUse(k, balls=[k.ports[0]], opened=[k.ports[1]]).y_at(f)
    zs, zc = R + 1j * w * L, 1 / (1j * w * C) + ESR
    assert np.allclose(opened[0, 0, 0], 1 / (zs + zc), rtol=1e-6)    # through to the die cap
    shorted = PackageUse(k, balls=[k.ports[0]], shorted=[k.ports[1]]).y_at(f)
    assert np.allclose(shorted[0, 0, 0], 1 / zs, rtol=1e-6)


# ---------------------------------------------------------------- the solve
def test_die_probe_through_the_package(tmp_path):
    snap = plane_pair()
    setup = plane_setup(snap, **SWEEP)
    zb0 = solve_ac(snap, setup, "VRM1", "GND").probes[0].z
    setup.packages.append(_package(tmp_path))
    assert sync_die_probes(setup)
    die = setup.probes[1]
    assert (die.name, die.package, die.die_port, die.on_die) == ("PKG +3V3_DIE", "PKG", 2, True)
    assert (die.rail.ref, die.rail.net, die.ground.net) == ("U1", "+3V3", "GND")
    assert not validate_ac(setup, "VRM1", "GND")
    r = solve_ac(snap, setup, "VRM1", "GND")
    assert [c.name for c in r.probes] == ["U1", "PKG +3V3_DIE"]
    w = 2 * np.pi * r.freqs
    zs, zc = R + 1j * w * L, 1 / (1j * w * C) + ESR
    assert np.allclose(r.probes[1].z, 1 / (1 / zc + 1 / (zs + zb0)), rtol=1e-6)
    assert np.allclose(r.probes[0].z, 1 / (1 / zb0 + 1 / (zs + zc)), rtol=1e-6)
    marks = {p.ref: p.text for p in r.board_map.probes}
    assert marks["PKG +3V3_DIE"] == "probe PKG +3V3_DIE on the die of U1"
    assert not any("held" in w for w in r.warnings)


def test_a_die_probe_alone_and_a_model_too_narrow(tmp_path):
    snap = plane_pair()
    setup = plane_setup(snap, **SWEEP)
    setup.probes.clear()
    k = _package(tmp_path)
    keep = (FF >= 1e6)
    k.touchstone = _text(FF[keep], _y_series_die(FF[keep]))
    setup.packages.append(k)
    sync_die_probes(setup)
    r = solve_ac(snap, setup, "VRM1", "GND")
    assert [c.name for c in r.probes] == ["PKG +3V3_DIE"]
    assert any("covers 1 MHz to 1 GHz" in w and "held" in w for w in r.warnings)


def test_ports_on_another_rail_are_left_open(tmp_path):
    snap = plane_pair()
    setup = plane_setup(snap, **SWEEP)
    k = _package(tmp_path)
    k.ports[1] = PackagePort(2, "VAUX", PCB, "VAUX", [], "GND", [])
    setup.packages.append(k)
    r = solve_ac(snap, setup, "VRM1", "GND")
    assert any("VAUX" in w and "left open" in w for w in r.warnings)


def test_validation(tmp_path):
    snap = plane_pair()
    setup = plane_setup(snap, **SWEEP)
    k = _package(tmp_path)
    setup.packages += [k, PackageModel("PKG", "", "# Hz S RI\n", 1)]
    errors = validate_ac(setup, "VRM1", "GND")
    assert "Package model names must be unique: PKG" in errors
    assert any("cannot be read" in e for e in errors)
    setup.packages.pop()
    k.ports.append(PackagePort(3, "", DIE, "", [], "", []))
    k.ports[0].role = OPEN
    errors = validate_ac(setup, "VRM1", "GND")
    assert any("port 3 is not in its 2-port model" in e for e in errors)
    assert any("connect at least one port to the PCB" in e for e in errors)


# ---------------------------------------------------------------- the setup
def test_saved_with_the_setup_and_die_probes_follow(tmp_path):
    snap = plane_pair()
    setup = plane_setup(snap)
    setup.packages.append(_package(tmp_path))
    sync_die_probes(setup)
    setup.probes[1].target_ohm = 0.02
    back = PISetup.load(setup.save(tmp_path / "s.yml"))
    k = back.packages[0]
    assert (k.name, k.ref, k.file, k.n_ports) == ("PKG", "U1", "pkg.s2p", 2)
    assert [(p.number, p.role, p.net, p.pins) for p in k.ports] == [
        (1, PCB, "+3V3", ["1"]), (2, DIE, "+3V3", [])]
    assert np.allclose(k.network().s, setup.packages[0].network().s)
    assert (back.probes[1].package, back.probes[1].target_ohm) == ("PKG", 0.02)
    assert back.part_refs() >= {"U1"}
    # Renamed, the probe follows; its port left open, its probe goes; removed, it goes too.
    rename_package(back, "PKG", "U1 package")
    back.packages[0].name = "U1 package"
    assert not sync_die_probes(back) and back.probes[1].package == "U1 package"
    back.packages[0].ports[1].role = SHORT
    assert sync_die_probes(back) and len(back.probes) == 1
    back.packages[0].ports[1].role = DIE
    sync_die_probes(back)
    del back.probes[1]
    assert open_removed_dies(back) and back.packages[0].ports[1].role == OPEN


def test_guess_ports_by_name():
    snap = plane_pair()
    k = PackageModel("PKG", "U1")
    ports = guess_ports(k, snap, ["+3V3_BGA", "3V3 die", "VAUX"], "GND")
    assert [(p.role, p.net, p.ground_net) for p in ports] == [
        (PCB, "+3V3", "GND"), (DIE, "+3V3", "GND"), (OPEN, "", "")]


def test_packages_tab(tmp_path):
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    from kipi.ac.gui.panel import ACPanel
    _app = QApplication.instance() or QApplication([])  # noqa: F841
    snap = plane_pair()
    setup = plane_setup(snap)
    panel = ACPanel()
    panel.set_board(snap, setup)
    k = _package(tmp_path, die=OPEN)
    panel.package_list.add(k)
    ed = panel.package_list.editor
    assert ed.ports.rowCount() == 2 and "2 ports" in ed.lbl_file.text()
    assert "<img" in ed.ports.toolTip()
    role = ed.ports.cellWidget(1, 2)
    role.setCurrentIndex(role.findData(DIE))                 # a die probe appears
    assert k.ports[1].role == DIE
    assert [p.name for p in setup.probes] == ["U1", "PKG +3V3_DIE"]
    assert not ed.ports.cellWidget(1, 4).isEnabled()           # no pins on the die
    panel.tabs.setCurrentWidget(panel.probe_list)
    panel.probe_list.lst.setCurrentRow(1)
    pe = panel.probe_list.editor
    assert "Port 2 of package PKG" in pe.lbl_die.text()
    assert pe.form.isRowVisible(pe.lbl_die) is True
    assert pe.form.isRowVisible(pe.rail) is False
    pe.target.setValue(20.0)
    assert setup.probes[1].target_ohm == pytest.approx(0.02)
    assert setup.probes[1].rail.pins == []                    # untouched by the editor
    panel.probe_list._remove()                                # its port is left open
    assert k.ports[1].role == OPEN and len(setup.probes) == 1
    panel.close()
