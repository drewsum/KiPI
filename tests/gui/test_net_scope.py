"""The nets ticked in the Board panel are part of the setup: saved, restored, and kept
when the board is re-read from KiCad. Only a board with no choice yet gets a guess."""

import json
import os
import time

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.core import yaml_io  # noqa: E402
from kipi.dc.pi.model import PISetup, setup_path  # noqa: E402
from phase_boards import board, two_phase  # noqa: E402

pytestmark = pytest.mark.usefixtures("no_3d")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _board(tmp_path):
    snap = board()
    snap.project_dir = str(tmp_path)
    return snap


def _idle(app, w, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if not w._busy() and not w._sync_timer.isActive():
            return
        time.sleep(0.01)
    raise AssertionError("window did not go idle")


def _window(app, snap):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    w._set_snapshot(snap)
    _idle(app, w)
    return w


def _tick(app, w, nets):
    w._set_checked(lambda it: it.data(0x0100) in nets)    # Qt.UserRole
    _idle(app, w)


def test_setup_keeps_never_chosen_apart_from_none_ticked():
    for nets in (None, [], ["VIN", "GND"]):
        assert PISetup.from_dict(json.loads(json.dumps(PISetup(nets=nets).to_dict()))).nets \
            == nets


def test_chosen_nets_are_saved_and_restored(app, tmp_path):
    w = _window(app, _board(tmp_path))
    _tick(app, w, {"VIN"})
    w._save_setup()
    saved = yaml_io.loads(setup_path("multiphase", str(tmp_path)).read_text(encoding="utf-8"))
    assert saved["nets"] == ["VIN"]
    w.close()

    w = _window(app, _board(tmp_path))         # the plugin started again
    assert w._checked_nets() == ["VIN"]
    w.close()


def test_unticking_every_net_is_restored_as_empty_not_guessed(app, tmp_path):
    w = _window(app, _board(tmp_path))
    _tick(app, w, set())
    w._save_setup()
    w.close()
    w = _window(app, _board(tmp_path))
    assert w._checked_nets() == []
    w.close()


def test_reloading_from_kicad_keeps_unsaved_ticks(app, tmp_path):
    PISetup(nets=["VIN"]).save(setup_path("multiphase", str(tmp_path)))
    w = _window(app, _board(tmp_path))
    _tick(app, w, {"VOUT", "GND"})             # changed, not saved
    w._set_snapshot(_board(tmp_path))          # "Reload from KiCad"
    _idle(app, w)
    assert set(w._checked_nets()) == {"VOUT", "GND"}
    assert set(w.solve_panel.setup.nets) == {"VOUT", "GND"}
    w.close()


def test_a_setup_saved_before_nets_were_stored_ticks_the_nets_it_uses(app, tmp_path):
    d = two_phase(ground=True).to_dict()
    del d["nets"]
    (tmp_path / "multiphase.kipi-setup.json").write_text(json.dumps(d), encoding="utf-8")
    w = _window(app, _board(tmp_path))
    assert {"VIN", "VOUT", "GND"} <= set(w._checked_nets())
    w.close()


def test_a_json_setup_is_read_with_a_warning_and_saved_as_yaml(app, tmp_path):
    # #76: JSON setups still open, are deprecated, and the next save writes YAML only.
    legacy = tmp_path / "multiphase.kipi-setup.json"
    legacy.write_text(json.dumps(PISetup(nets=["VIN"]).to_dict()), encoding="utf-8")
    w = _window(app, _board(tmp_path))
    assert w._checked_nets() == ["VIN"]
    assert "deprecated" in w.log_view.toPlainText()
    assert "saved as multiphase.kipi-setup.yml" in w.setup_bar.lbl_file.text()
    _tick(app, w, {"VIN", "VOUT"})
    w._save_setup()
    w.close()
    yml = tmp_path / "multiphase.kipi-setup.yml"
    assert set(yaml_io.loads(yml.read_text(encoding="utf-8"))["nets"]) == {"VIN", "VOUT"}
    assert json.loads(legacy.read_text(encoding="utf-8"))["nets"] == ["VIN"]   # untouched
    w = _window(app, _board(tmp_path))         # the YAML wins from then on
    assert set(w._checked_nets()) == {"VIN", "VOUT"}
    assert "deprecated" not in w.log_view.toPlainText()
    w.close()
