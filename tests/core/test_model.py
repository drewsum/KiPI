import math

import pytest

from kipi.core.geometry.copper import net_copper, net_layer_copper
from kipi.core.kicad_io import snapshot
from kipi.core.kicad_io.convert import arc_points as _arc_points
from kipi.core.model.nets import classify_nets, guess_voltage, is_ground_name, is_power_name
from synthetic import B, F, IN1, IN2, make_board

# The copper module approximates circles with 32-gon polygons.
def circle(r):
    return 0.5 * 32 * r * r * math.sin(2 * math.pi / 32)


def test_stackup_z_order():
    st = make_board().stackup
    zs = [c.z_mid_mm for c in st.copper]
    assert zs == sorted(zs, reverse=True)
    assert st.copper[0].z_top_mm == pytest.approx(st.board_thickness_mm)
    assert st.copper[-1].z_bottom_mm == pytest.approx(0.0)
    assert st.layers_between(B, IN1) == [IN1, IN2, B]


def test_copper_areas():
    snap = make_board()
    drill = circle(0.15)
    ring = circle(0.3) - drill
    # track 10 x 0.5 + two 1x1 pads (each overlaps the track by 0.5 x 0.5); via inside pad
    assert net_layer_copper(snap, "+3V3", F).area == pytest.approx(6.5 - drill, abs=1e-6)
    assert net_layer_copper(snap, "+3V3", IN1).area == pytest.approx(ring, abs=1e-6)
    assert net_layer_copper(snap, "+3V3", IN2).area == pytest.approx(200 - drill, abs=1e-6)
    assert net_layer_copper(snap, "+3V3", B).area == pytest.approx(ring, abs=1e-6)
    assert net_layer_copper(snap, "GND", IN1).area == pytest.approx(199.0, abs=1e-6)
    assert set(net_copper(snap, "GND")) == {F, IN1}


def test_snapshot_roundtrip(tmp_path):
    snap = make_board()
    for name in ("b.json", "b.json.gz"):
        path = snapshot.save(snap, tmp_path / name)
        back = snapshot.load(path)
        for a, b in zip(back.stackup.copper, snap.stackup.copper):
            assert (a.id, a.name, a.index) == (b.id, b.name, b.index)
            assert a.z_bottom_mm == pytest.approx(b.z_bottom_mm, abs=1e-6)
            assert a.thickness_mm == pytest.approx(b.thickness_mm)
        assert back.pads == snap.pads
        assert back.zones == snap.zones
        assert back.tracks == snap.tracks
        assert back.vias == snap.vias
        assert net_layer_copper(back, "+3V3", F).area == pytest.approx(
            net_layer_copper(snap, "+3V3", F).area)


def test_arc_points_pass_through_mid():
    pts = _arc_points((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), include_start=True)
    assert pts[0] == (1.0, 0.0) and pts[-1] == (-1.0, 0.0)
    assert all(math.hypot(x, y) == pytest.approx(1.0) for x, y in pts)
    assert max(y for _, y in pts) == pytest.approx(1.0, abs=1e-2)   # upper half
    pts = _arc_points((1.0, 0.0), (0.0, -1.0), (-1.0, 0.0))
    assert min(y for _, y in pts) == pytest.approx(-1.0, abs=1e-2)  # lower half


@pytest.mark.parametrize("name", ["+3V3", "VCC", "/VDD_CORE", "+5V", "12V", "VBUS", "1V8",
                                  "VIN", "-12V", "V3P3"])
def test_power_names(name):
    assert is_power_name(name)


@pytest.mark.parametrize("name", ["Net-(U1-Pad3)", "SDA", "/MCU/RESET", "LED1", "CLK_25M"])
def test_signal_names(name):
    assert not is_power_name(name) and not is_ground_name(name)


def test_classify():
    kinds = classify_nets(make_board())
    assert kinds == {"+3V3": "power", "GND": "ground", "Net-(U1-Pad3)": "signal"}


@pytest.mark.parametrize("name,volts", [("+3.3V", 3.3), ("3V3", 3.3), ("V3P3", 3.3), ("1V8", 1.8),
                                        ("+12V", 12.0), ("+1.2V_VCORE", 1.2), ("+5V_USB", 5.0),
                                        ("GND", 0.0), ("GNDA_DSP", 0.0), ("VBUS", 5.0),
                                        ("POWER_BUTTON", 1.0)])
def test_guess_voltage(name, volts):
    assert guess_voltage(name) == pytest.approx(volts)
