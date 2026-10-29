"""Capacitor model files (#229): a model exported to YAML, named after it, and imported
into another setup with everything but the parts that use it."""

import json
import os

import pytest

from kipi.ac import cap_file
from kipi.ac.model import CapModel
from kipi.core import yaml_io
from kipi.core.model.spice import SpiceRef
from kipi.dc.pi.model import PISetup

NETLIST = ".SUBCKT C22U 1 2\nC1 1 3 22u\nL1 3 4 0.4n\nR1 4 2 2m\n.ENDS\n"


def _model():
    return CapModel("GRM188R61A226ME15", 22e-6, 0.4e-9, 2e-3, ["C1", "C2"], rated_v=10.0,
                    kept_at_rated_pct=25.0, dielectric="X5R", aging_pct_per_decade=3.0,
                    mfg_tolerance_pct=20.0,
                    spice=SpiceRef("C22U", NETLIST, "grm.mod", True,
                                   {"capacitance_f": 22e-6, "esl_h": 0.4e-9,
                                    "esr_ohm": 2e-3}),
                    temp_c=60.0)


def test_a_model_round_trips_without_its_parts(tmp_path):
    m = _model()
    path = cap_file.write(tmp_path / cap_file.file_name(m), m)
    assert path.name == "GRM188R61A226ME15.yml"
    text = path.read_text(encoding="utf-8")
    assert "C1" not in text.split("netlist")[0] and "temp_c" not in text
    back = cap_file.read(path)
    assert back.refs == [] and back.temp_c is None
    for k in ("name", "capacitance_f", "esl_h", "esr_ohm", "rated_v", "kept_at_rated_pct",
              "dielectric", "aging_pct_per_decade", "mfg_tolerance_pct"):
        assert getattr(back, k) == getattr(m, k), k
    assert back.spice == m.spice


def test_the_file_is_yaml_and_json_works_without_pyyaml(tmp_path, monkeypatch):
    m = _model()
    path = cap_file.write(tmp_path / "a.yml", m)
    if yaml_io.available():
        assert yaml_io.loads(path.read_text(encoding="utf-8"))["kind"] == cap_file.KIND
    monkeypatch.setattr(yaml_io, "available", lambda: False)
    path = cap_file.write(tmp_path / "b.yml", m)
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == m.name
    assert cap_file.read(path).spice == m.spice


def test_names_a_file_cannot_hold_are_replaced():
    assert cap_file.file_name(CapModel('22u/10V "X5R"')) == "22u_10V _X5R.yml"
    assert cap_file.file_name(CapModel("...")) == "capacitor.yml"


def test_other_files_are_refused(tmp_path):
    p = tmp_path / "x.yml"
    p.write_text("name: C1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Not a KiPI capacitor model"):
        cap_file.read(p)
    p.write_text(json.dumps({**cap_file.to_dict(_model()), "format": 99}), encoding="utf-8")
    with pytest.raises(ValueError, match="newer"):
        cap_file.read(p)
    p.write_text("{: not yaml", encoding="utf-8")
    with pytest.raises(ValueError):
        cap_file.read(p)


def test_the_setup_reads_its_models_as_before():
    s = PISetup()
    s.caps.append(_model())
    back = PISetup.from_dict(s.to_dict()).caps[0]
    assert back.refs == ["C1", "C2"] and back.spice == s.caps[0].spice


# --- GUI -----------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("module", ["kipi.ac.gui.panel", "kipi.noise.gui.panel"])
def test_the_capacitors_tab_exports_and_imports(app, tmp_path, monkeypatch, module):
    import importlib

    from qtpy.QtWidgets import QFileDialog, QPushButton
    mod = importlib.import_module(module)
    panel = mod.ACPanel() if module.startswith("kipi.ac") else mod.NoisePanel()
    panel.setup.caps.append(_model())
    panel.setEnabled(True)
    panel.cap_list.reload()
    panel.cap_list.lst.setCurrentRow(0)
    buttons = {b.text(): b for b in panel.cap_list.findChildren(QPushButton)}
    out = tmp_path / "GRM188R61A226ME15.yml"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    buttons["Export…"].click()
    assert out.is_file()
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(out)], ""))
    buttons["Import…"].click()
    names = [m.name for m in panel.setup.caps]
    assert names == ["GRM188R61A226ME15", "GRM188R61A226ME15 (2)"]
    assert panel.setup.caps[1].refs == [] and panel.cap_list.current() is panel.setup.caps[1]
