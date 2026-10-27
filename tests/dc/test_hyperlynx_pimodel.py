"""HyperLynx PI model files (#149): a part's DC sink currents pin by pin, read as a load
model split by the net each pin is on."""

import pytest

from kipi.dc.pi.importers.hyperlynx_pimodel import read_sinks
from kipi.dc.pi.load_model import LoadModel, ModelError, apply_to_load, new_items, on_pins
from kipi.dc.pi.model import Load, PinRef

# The example from the issue, with a VRM record and a pin that draws more.
SAMPLE = """\
* HL PI Model File
* Record format:
* <pin name> DC I=<value> R=<value>
* or
* <pin name> VRM TYPE=SIMPLE V=<value> R=<value> L=<value>
* All parameters are optional, default values are used for missed parameters
*
VERSION=1.0
15 DC I=0.5 R=1e+06
28 DC I=0.5 R=1e+06
36 DC I=0.5 R=1e+06
41 DC I=0.5 R=1e+06
46 DC I=1.5 R=1e+06
50 VRM TYPE=SIMPLE V=1.0 R=0.001 L=1n

"""


@pytest.fixture
def model(tmp_path):
    path = tmp_path / "U7.hlpimodel"
    path.write_text(SAMPLE, encoding="utf-8")
    return LoadModel.load(path)


def test_the_dc_sinks_read_as_one_supply_and_vrm_records_are_skipped(model):
    assert model.part == "U7" and len(model.supplies) == 1
    s = model.supplies[0]
    assert s.split_by_net and s.pins == ["15", "28", "36", "41", "46"]
    assert {g.name: (g.pins, g.current) for g in s.groups} == {
        "0.5 A per pin": (["15", "28", "36", "41"], 2.0), "1.5 A per pin": (["46"], 1.5)}
    assert any("VRM records skipped" in n and "50" in n for n in model.notes)


def test_values_take_spice_scales_and_missing_or_resistive_ones_are_noted():
    amps, notes = read_sinks("VERSION=1.0\nA1 DC I=250m R=10\nA2 DC R=1e6\nA3 DC I=-2u\n")
    assert amps == {"A1": pytest.approx(0.25), "A2": 0.0, "A3": pytest.approx(-2e-6)}
    assert any("0 A" in n and "A2" in n for n in notes)
    assert any("resistance" in n and "A1" in n and "A2" not in n for n in notes)
    with pytest.raises(ModelError):
        read_sinks("A1 DC I=lots\n")


def test_a_file_that_is_not_a_pi_model_is_refused(tmp_path):
    path = tmp_path / "x.hlpimodel"
    path.write_text("hello\n", encoding="utf-8")
    with pytest.raises(ModelError):
        LoadModel.load(path)


def test_added_loads_get_one_per_net_with_each_pin_drawing_its_own(model):
    nets = {"+1V0": ["15", "28", "46"], "+1V8": ["36", "41"], "GND": ["1", "2"]}
    items = {s.name: (s, rail) for s, rail, _gnd in new_items(model, "U7", nets)}
    assert set(items) == {"+1V0", "+1V8"}
    core, rail = items["+1V0"]
    assert rail == PinRef("U7", "+1V0", ["15", "28", "46"])
    assert sorted((g.pins, g.current) for g in core.groups) == [
        (["15", "28"], 1.0), (["46"], 1.5)]
    aux, _ = items["+1V8"]                     # one current left: a plain total
    assert (aux.current_a, aux.groups) == (1.0, [])


def test_applied_to_a_load_only_its_own_pins_count(model):
    load = Load("U7 aux", PinRef("U7", "+1V8", ["36", "41"]), 0.0)
    apply_to_load(load, on_pins(model.supplies[0], load.supply.pins))
    assert (load.current, load.groups) == (1.0, [])
