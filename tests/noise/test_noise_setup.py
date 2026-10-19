"""The Coupled Noise setup (#69): aggressors in the setup file, their operating point,
checks and the phases guessed from the board."""

import pytest

from kipi.core import yaml_io
from kipi.core.model.parts import parse_inductance
from kipi.dc.pi.model import PinRef, PISetup, Vrm
from kipi.noise.checks import (
    NoiseError, operating_point, seed_phases, validate_noise,
)
from kipi.noise.model import Aggressor, AggressorPhase
from noise_boards import buck_board, buck_setup


@pytest.mark.parametrize("text, henries", [
    ("4.7uH", 4.7e-6), ("10 µH 3A", 10e-6), ("1u0", 1e-6), ("470n", 470e-9),
    ("2R2", 2.2e-6), ("0.47", 0.47e-6), ("1mH", 1e-3), ("150 nH", 150e-9),
    ("10R", None), ("abc", None), ("", None)])
def test_parse_inductance(text, henries):
    got = parse_inductance(text)
    assert got == (pytest.approx(henries) if henries is not None else None)


def test_aggressors_round_trip_through_the_setup_file():
    s = PISetup(aggressors=[Aggressor(
        "A1", "U1", topology="buck", vin_v=12.0, fsw_hz=1e6, rise_s=3e-9, duty=0.2,
        phases=[AggressorPhase("L1", 1e-6, 0.002, "SW", PinRef("U1", "VIN", ["1"]),
                               PinRef("U1", "GND", []), ["C1"], ["C2"], coss_f=1e-10,
                               rds_on_ls_ohm=0.004, package_l_h=0.2e-9)])])
    s.noise.max_harmonics = 1234
    back = PISetup.from_dict(yaml_io.loads(yaml_io.dumps(s.to_dict())))
    assert back == s
    assert {"L1", "U1"} <= back.part_refs()
    assert {"VIN", "GND"} <= back.pin_nets()


def test_an_older_setup_has_no_aggressors():
    s = PISetup.from_dict({"vrms": []})
    assert s.aggressors == [] and s.noise.max_harmonics == 4000


def test_operating_point_of_a_buck_from_the_setup():
    setup = buck_setup(load_a=5.0)
    op = operating_point(setup, Aggressor("A", "U1"))
    assert op.topology == "buck"
    assert (op.vin, op.vout, op.iout) == (12.0, 1.8, 5.0)
    assert op.duty == pytest.approx(1.8 / 12.0)       # no losses entered
    assert op.iin == pytest.approx(1.8 * 5.0 / (0.9 * 12.0))


def test_entered_values_win():
    setup = buck_setup()
    op = operating_point(setup, Aggressor("A", "U1", vin_v=10.0, load_a=3.0, duty=0.25))
    assert (op.vin, op.iout, op.duty) == (10.0, 3.0, 0.25)


def test_losses_raise_a_bucks_duty():
    setup = buck_setup(load_a=10.0)
    a = Aggressor("A", "U1", phases=[AggressorPhase(dcr_ohm=0.01, rds_on_ls_ohm=0.01,
                                                    rds_on_hs_ohm=0.01)])
    assert operating_point(setup, a).duty == pytest.approx((1.8 + 10 * 0.02) / 12.0)


def test_a_boost_is_found_from_its_voltages():
    setup = PISetup()
    setup.vrms.append(Vrm("SRC", PinRef("J1", "VBAT"), 3.7))
    setup.vrms.append(Vrm("U5", PinRef("L5", "V12"), 12.0, kind="smps",
                          input=PinRef("U5", "VBAT")))
    op = operating_point(setup, Aggressor("A", "U5", load_a=0.5))
    assert op.topology == "boost"
    assert op.duty == pytest.approx(1 - 3.7 * 0.9 / 12.0)
    with pytest.raises(NoiseError, match="cannot"):
        operating_point(setup, Aggressor("A", "U5", topology="buck", load_a=0.5))


def test_checks_name_what_is_missing():
    setup = buck_setup()
    snap = buck_board()
    errors = validate_noise(setup, snap, Aggressor("A", "U1"))
    assert any("phases" in e for e in errors)
    errors = validate_noise(setup, snap, Aggressor("A", "J1", phases=[AggressorPhase()]))
    assert any("not a switcher" in e for e in errors)
    errors = validate_noise(setup, snap, Aggressor(
        "A", "U1", phases=[AggressorPhase("L9", 1e-6)]))
    assert any("not on the board: L9" in e for e in errors)
    errors = validate_noise(setup, snap, Aggressor("A", "U1", phases=[AggressorPhase()]))
    assert any("inductance" in e for e in errors)


def test_phases_are_filled_from_the_board():
    setup, snap = buck_setup(), buck_board()
    vrm = next(v for v in setup.vrms if v.name == "U1")
    (ph,) = seed_phases(snap, setup, vrm, "buck")
    assert (ph.inductor, ph.switch_net, ph.inductance_h) == ("L1", "SW", pytest.approx(1e-6))
    assert ph.high_side == PinRef("U1", "VIN", ["1"])
    assert ph.low_side == PinRef("U1", "GND", ["2"])
    assert ph.input_caps == ["C1"] and ph.output_caps == ["C2"]
