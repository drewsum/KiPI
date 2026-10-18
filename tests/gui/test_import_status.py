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
        assert "not saved" in w.solve_panel.lbl_file.text()
        assert "#ff8c00" in w.solve_panel.btn_save.styleSheet()        # Save is outlined
        w.close()
        assert any("not saved" in q for q in asked)
        assert setup_file.read_text(encoding="utf-8") == "the user's own setup"
    finally:
        w.close()
