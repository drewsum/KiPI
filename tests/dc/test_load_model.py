"""Load models (#17): a part's supplies in a file of their own, exported from the loads and
probes on one reference designator and applied to any number of loads and probes."""

import pytest

from kipi.ac.model import Probe
from kipi.dc.pi.load_model import (
    LoadModel, ModelError, SupplyModel, apply_to_load, apply_to_probe, best_supply, model_of,
    model_refs, new_items,
)
from kipi.dc.pi.model import Load, PinGroup, PinRef, PISetup, Vrm

YAML = """\
kipi_load_model: 1
part: FPGA-X
supplies:
  - name: VCCINT
    pins: [F7, F9, H7]
    ground_pins: [A1]
    pin_groups:
      - {name: core, pins: [F7, F9], current_a: 0.8}
      - {pins: [H7], current_a: 0.4}
    tolerance: {min: -3, max: 3, unit: "%"}
    heat_w: 0.9                 # from before #121: read and ignored
    load_step: {step_a: 0.6, slew_a_per_us: 5}
  - name: VCCAUX
    pins: [K1]
    current_a: 0.1
    tolerance: {min: -0.09, max: 0.09, unit: "±V"}
    target: {points: [[1.0e3, 0.1], [1.0e7, 0.2]]}
"""


def _setup():
    s = PISetup()
    s.vrms = [Vrm("U1", PinRef("U1", "+1V0", ["2"]), 1.0, "source")]
    s.loads = [Load("U5 VCCINT", PinRef("U5", "+1V0", ["F7", "F9", "H7"]), 1.2,
                    ground=PinRef("U5", "GND", ["A1"]), tol_min=-3.0, tol_max=3.0,
                    groups=[PinGroup(["F7", "F9"], 0.8, "core"), PinGroup(["H7"], 0.4)]),
               Load("U5", PinRef("U5", "+1V8", ["K1"]), 0.1, tol_min=1.71, tol_max=None,
                    tol_pct=False),
               Load("U6", PinRef("U6", "+1V0", []), 2.0)]
    s.probes = [Probe("U5 core", PinRef("U5", "+1V0", ["F7"]), PinRef("U5", "GND", ["A1"]),
                      target_ohm=0.01, target_hz=5e7),
                Probe("U5 io", PinRef("U5", "+3V3", ["M2"]), PinRef("U5", "GND", ["A1"]),
                      target_custom=True, target_points=[(1e6, 0.2), (1e3, 0.1)])]
    return s


def test_a_model_file_reads_and_writes_back(tmp_path):
    path = tmp_path / "fpga.kipi-load.yml"
    path.write_text(YAML, encoding="utf-8")
    m = LoadModel.load(path)
    assert m.part == "FPGA-X" and [s.name for s in m.supplies] == ["VCCINT", "VCCAUX"]
    core, aux = m.supplies
    assert [g.current for g in core.groups] == [0.8, 0.4] and core.groups[0].name == "core"
    assert (core.tol_min, core.tol_max, core.tol_unit) == (-3.0, 3.0, "%")
    assert (aux.tol_unit, aux.target_points) == ("±V", [(1e3, 0.1), (1e7, 0.2)])
    assert (core.step_a, core.slew_a_per_us) == (0.6, 5.0)
    again = LoadModel.load(m.save(tmp_path / "again.yml"))
    assert again == m
    assert m.save(tmp_path / "again.json") == tmp_path / "again.json.yml"   # YAML only


def test_only_yaml_files_are_load_models(tmp_path, monkeypatch):
    path = tmp_path / "model.json"
    path.write_text('{"kipi_load_model": 1, "supplies": []}', encoding="utf-8")
    with pytest.raises(ModelError, match="YAML files"):
        LoadModel.load(path)
    from kipi.core import yaml_io
    monkeypatch.setattr(yaml_io, "yaml", None)
    with pytest.raises(ModelError, match="need PyYAML"):
        LoadModel().save(tmp_path / "x.yml")


@pytest.mark.parametrize("text, why", [
    ("part: x\n", "not a KiPI load model"),
    ("kipi_load_model: 99\n", "newer version"),
    ("kipi_load_model: 1\nsupplies: [{pins: [1]}]\n", "needs a name"),
    ("kipi_load_model: 1\nsupplies: [{name: A, current_a: lots}]\n", "must be a number"),
    ("kipi_load_model: 1\nsupplies: [{name: A, tolerance: {max: 1, unit: mV}}]\n", "unit"),
    ("kipi_load_model: [1\n", "not valid YAML"),
])
def test_bad_files_say_why(tmp_path, text, why):
    path = tmp_path / "bad.yml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ModelError, match=why):
        LoadModel.load(path)


def test_export_takes_every_load_and_probe_on_the_part():
    s = _setup()
    assert model_refs(s) == ["U5", "U6"]
    m = model_of(s, "U5", "FPGA-X")
    assert [x.name for x in m.supplies] == ["VCCINT", "+1V8", "io"]
    core, aux, io = m.supplies
    assert core.current_a is None and len(core.groups) == 2       # groups carry the current
    assert (core.pins, core.ground_pins) == (["F7", "F9", "H7"], ["A1"])
    assert (core.target_ohm, core.target_hz) == (0.01, 5e7)       # the probe on its net
    assert (aux.current_a, aux.tol_min, aux.tol_max, aux.tol_unit) == (0.1, 1.71, None, "V")
    assert io.current_a is None and io.target_points == [(1e3, 0.1), (1e6, 0.2)]


def test_apply_to_many_loads_and_probes():
    m = model_of(_setup(), "U5")
    loads = [Load(f"U{i} VCCINT", PinRef(f"U{i}", "+1V0", ["F7", "F9", "H7"]), 9.0)
             for i in (7, 8, 9)]
    for l in loads:
        i = best_supply(m, l.name, l.supply)
        assert i == 0
        assert apply_to_load(l, m.supplies[i]) == ["pin groups", "tolerance"]
        assert l.total_current() == pytest.approx(1.2) and l.tol_min == -3.0
    aux = Load("U7 aux", PinRef("U7", "+1V8", []), 9.0, tol_min=-5.0, tol_max=5.0)
    assert best_supply(m, aux.name, aux.supply) == 1                 # by its net's name
    apply_to_load(aux, m.supplies[1])
    assert (aux.current, aux.tol_min, aux.tol_max, aux.tol_pct) == (0.1, 1.71, None, False)
    assert best_supply(m, "nothing alike", PinRef("U7", "VX", [])) is None
    p = Probe("P", PinRef("U7", "+3V3", []), PinRef("U7", "GND", []))
    assert apply_to_probe(p, m.supplies[2]) == ["target mask"]
    assert p.target_custom and p.target_points == [(1e3, 0.1), (1e6, 0.2)]


def test_a_probe_target_from_the_load_step():
    s = _setup()
    sup = SupplyModel("VCCINT", step_a=0.5, slew_a_per_us=10.0, tol_min=-3.0, tol_max=2.0)
    p = Probe("P", PinRef("U7", "+1V0", []), PinRef("U7", "GND", []))
    assert apply_to_probe(p, sup, s) == ["target from the load step", "load step"]
    assert (p.step_a, p.slew_a_per_us) == (0.5, 10.0)          # for its transient (#166)
    assert p.target_ohm == pytest.approx(0.02 / 0.5)        # the tighter side: 2 % of 1 V
    assert p.target_hz == pytest.approx(1 / (3.141592653589793 * 0.05e-6))
    sup.ac_tolerance_mv = 10.0
    apply_to_probe(p, sup, s)
    assert p.target_ohm == pytest.approx(0.02)
    unknown = Probe("Q", PinRef("U7", "+5V", []), PinRef("U7", "GND", []))
    assert apply_to_probe(unknown, SupplyModel("X", step_a=1, slew_a_per_us=1), s) ==         ["load step"]                                       # no tolerance: no target


def test_new_loads_for_another_copy_of_the_part():
    m = LoadModel.from_dict({"kipi_load_model": 1, "supplies": [
        {"name": "VCCINT", "pins": ["F7", "F9"], "ground_pins": ["A1"]},
        {"name": "VCCAUX", "pins": ["K1"]}, {"name": "VNONE"}]})
    nets = {"+1V0": ["F7", "F9", "Z1"], "GND": ["A1", "A2"], "+1V8": ["K2"]}
    items = new_items(m, "U9", nets)
    assert len(items) == 1
    s, rail, gnd = items[0]
    assert s.name == "VCCINT" and rail == PinRef("U9", "+1V0", ["F7", "F9"])
    assert gnd == PinRef("U9", "GND", ["A1"])
