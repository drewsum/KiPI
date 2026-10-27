"""The Model menu of the DC Loads and AC Probes lists (#17)."""

import os

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QDialog  # noqa: E402

from kipi.dc.pi.load_model import LoadModel  # noqa: E402
from kipi.gui import load_model_dialogs as dialogs  # noqa: E402
from tree_boards import board, setup  # noqa: E402

MODEL = LoadModel.from_dict({"kipi_load_model": 1, "part": "REG", "supplies": [
    {"name": "+3V3", "pins": ["1"], "ground_pins": ["2"], "current_a": 0.25,
     "tolerance": {"min": -0.1, "max": 0.1, "unit": "±V"},
     "load_step": {"step_a": 0.1, "slew_a_per_us": 1, "ac_tolerance_mv": 33}}]})


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def accept(monkeypatch):
    """Dialogs accept at once, with what they ticked by themselves (or every part)."""
    def tick_all(self):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(dialogs.Qt.Checked)
        return QDialog.Accepted
    monkeypatch.setattr(dialogs.ApplyDialog, "exec", lambda self: QDialog.Accepted)
    monkeypatch.setattr(dialogs.PartsDialog, "exec", tick_all)


def test_dc_loads_take_a_model_and_get_added_from_one(app, accept):
    from kipi.dc.gui.pi_panel import PIPanel
    panel = PIPanel()
    panel.snapshot, panel.setup = board(), setup("ldo")
    panel.load_list.reload()
    labels = [b.text() for b in panel.load_list.findChildren(type(panel.btn_solve))]
    assert "Model ▾" in labels
    panel.apply_load_model(MODEL)              # U2 on +3V3 pin 1: matched by its pins
    u2 = panel.setup.loads[0]
    assert (u2.current, u2.tol_min, u2.tol_dv) == (0.25, -0.1, True)
    panel.add_loads_from_model(MODEL)          # every part with pin 1 on a net
    added = {l.name: l for l in panel.setup.loads[1:]}
    assert "U2 +3V3" in added and added["U2 +3V3"].ground.pins == ["2"]
    assert all(l.current == 0.25 for l in added.values())


def test_ac_probes_take_the_target_from_the_load_step(app, accept):
    from kipi.ac.gui.panel import ACPanel
    panel = ACPanel()
    panel.snapshot, panel.setup = board(), setup("ldo")
    panel.add_probes_from_model(MODEL)
    p = next(p for p in panel.setup.probes if p.name == "U2 +3V3")
    assert p.target_ohm == pytest.approx(0.33) and not p.target_custom
    p.target_ohm = None
    panel.apply_load_model(MODEL)
    assert p.target_ohm == pytest.approx(0.33)


def test_apply_dialog_ticks_only_what_matches(app):
    d = dialogs.ApplyDialog(MODEL, [("U2", setup().loads[0].supply), ("X", None)], "load")
    assert d.chosen() == [(0, 0)]
    assert d.boxes[1].currentData() == 0              # the only supply, but not ticked
    d.checks[1].setChecked(True)
    assert d.chosen() == [(0, 0), (1, 0)]
    d.boxes[0].setCurrentIndex(0)                     # leave as is: untick
    assert not d.checks[0].isChecked() and d.chosen() == [(1, 0)]


def test_dc_loads_get_added_from_a_hyperlynx_model_one_per_net(app, accept, tmp_path):
    from kipi.dc.gui.pi_panel import PIPanel
    path = tmp_path / "U1.hlpimodel"
    path.write_text("* HL PI Model File\nVERSION=1.0\n1 DC I=0.2 R=1e+06\n"
                    "2 DC I=0.3 R=1e+06\n", encoding="utf-8")
    model = LoadModel.load(path)
    panel = PIPanel()
    panel.snapshot, panel.setup = board(), setup("ldo")
    panel.setup.loads = []
    panel.load_list.reload()
    panel.add_loads_from_model(model)          # U1 pin 1 on VIN, pin 2 on +3V3; U2 too
    loads = {l.name: l for l in panel.setup.loads}
    assert (loads["U1 VIN"].supply.pins, loads["U1 VIN"].current) == (["1"], 0.2)
    assert (loads["U1 +3V3"].supply.pins, loads["U1 +3V3"].current) == (["2"], 0.3)
    u2 = loads["U2 +3V3"]                       # pin 1 of U2 is on +3V3
    u2.current = 0.0
    panel.apply_load_model(model)
    assert u2.current == 0.2 and loads["U1 VIN"].current == 0.2
