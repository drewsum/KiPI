# Copyright (c) 2026 Tomachie LLC.
"""After a pdn.json import the Log says what was read and what changed, as its last line,
where it can be seen without hunting for a log file; the setup file is written only by
Save."""

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_import_says_what_it_read_and_what_changed(app, no_3d, tmp_path, monkeypatch):
    from kipi.gui.main_window import MainWindow
    sys.path.insert(0, str(Path(__file__).parents[1] / "dc"))     # the import tests' file
    from test_tomachie_pdn import PDN
    from tree_boards import board
    path = tmp_path / "board.pdn.json"
    path.write_text(json.dumps(PDN), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.Discard
                        if "not saved" in a[2] else QMessageBox.Yes)
    w = MainWindow()
    try:
        w.show()
        w.snapshot = board()
        w._load_setup(w.snapshot)
        setup_file = tmp_path / "kept.kipi-setup.yml"
        setup_file.write_text("the user's own setup", encoding="utf-8")
        w._setup_path = setup_file
        w.import_pdn()
        last = w.log_view.toPlainText().splitlines()[-1]
        assert last.startswith("== Read board.pdn.json: ") and "a new power tree" in last
        w.import_pdn()                                   # the same file again
        assert "the same as before" in w.log_view.toPlainText().splitlines()[-1]
        assert w.log_view.minimumHeight() > 0
        # Only Save writes the setup file: the import left it as it was, and leaving the
        # window asks about the changes.
        assert setup_file.read_text(encoding="utf-8") == "the user's own setup"
        assert "not saved" in w.setup_bar.lbl_file.text()
        assert "#ff8c00" in w.setup_bar.btn_save.styleSheet()        # Save is outlined
        w.close()
        assert any("not saved" in q for q in asked)
        assert setup_file.read_text(encoding="utf-8") == "the user's own setup"
    finally:
        w.close()


def test_a_solve_does_not_write_the_setup_file(app, no_3d, tmp_path, monkeypatch):
    from kipi.gui.main_window import MainWindow
    sys.path.insert(0, str(Path(__file__).parents[1] / "dc"))
    from tree_boards import board
    w = MainWindow()
    try:
        w.snapshot = board()
        w._load_setup(w.snapshot)
        setup_file = tmp_path / "kept.kipi-setup.yml"
        setup_file.write_text("the user's own setup", encoding="utf-8")
        w._setup_path = setup_file
        monkeypatch.setattr(w, "_run", lambda *a, **k: None)     # the solve itself is not run
        w.run_solve(w.solve_panel.setup)
        w.run_noise_solve(w.noise_panel.setup, "")
        assert setup_file.read_text(encoding="utf-8") == "the user's own setup"
    finally:
        w.close()


def test_a_background_start_imports_its_file_and_asks_nothing(app, no_3d, tmp_path,
                                                              monkeypatch):
    """--background --pdn-json: the file is imported once the board is in, with no
    dialog; a file that cannot be read is said in the Log, not in a dialog."""
    from kipi.gui.main_window import MainWindow
    sys.path.insert(0, str(Path(__file__).parents[1] / "dc"))
    from test_tomachie_pdn import PDN
    from tree_boards import board
    path = tmp_path / "board.pdn.json"
    path.write_text(json.dumps(PDN), encoding="utf-8")
    dialogs = []
    for kind in ("critical", "warning", "information", "question"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, **k: dialogs.append(a[2]))
    w = MainWindow(pdn_json=str(path), background=True)
    try:
        w.showMinimized()
        w._set_snapshot(board())
        app.processEvents()
        assert "== Read board.pdn.json: " in w.log_view.toPlainText()
        w.import_pdn_file(str(tmp_path / "missing.pdn.json"), quiet=w._quiet())
        assert "Could not import Tomachie missing.pdn.json" in w.log_view.toPlainText()
        assert dialogs == []
    finally:
        w.close()


def test_a_hyperlynx_session_file_ticks_its_power_nets_and_adds_its_links(
        app, no_3d, tmp_path, monkeypatch):
    from kipi.gui.main_window import MainWindow
    sys.path.insert(0, str(Path(__file__).parents[1] / "dc"))
    from test_hyperlynx_bud import BUD, _board
    path = tmp_path / "board.bud"
    path.write_text(BUD, encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard)
    w = MainWindow()
    try:
        w.show()
        w.snapshot = _board()
        w._load_setup(w.snapshot)
        w._setup_path = tmp_path / "kept.kipi-setup.yml"
        ticked = []

        class Item:                     # a net list row, unticked
            def __init__(self, net):
                self.net = net

            def checkState(self):
                return None

            def data(self, _role):
                return self.net

        monkeypatch.setattr(w, "_set_checked", lambda pred: ticked.extend(
            n.net for n in map(Item, ("VIN", "+3V3", "GND", "+5V")) if pred(n)))
        w.import_pdn()
        last = w.log_view.toPlainText().splitlines()[-1]
        assert last.startswith("== Read board.bud: ") and "3 power net(s) ticked" in last
        assert ticked == ["VIN", "+3V3", "GND"]
        assert [k.name for k in w.solve_panel.setup.links] == ["U1"]
        assert {m.name for m in w.solve_panel.setup.caps} == {"Group1", "Group2"}
    finally:
        w.close()
