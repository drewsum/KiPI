"""After a pdn.json import the window says what it read and what changed, in the status
bar and the Log, where it can be seen without hunting for a log file."""

import json
import os

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
    from test_tomachie_pdn import PDN
    from tree_boards import board
    path = tmp_path / "board.pdn.json"
    path.write_text(json.dumps(PDN), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    w = MainWindow()
    try:
        w.show()
        w.snapshot = board()
        w.import_pdn()
        first = w.statusBar().currentMessage()
        assert first.startswith("Read board.pdn.json: ") and "a new power tree" in first
        assert "== Read board.pdn.json" in w.log_view.toPlainText()
        w.import_pdn()                                   # the same file again
        assert "the same as before" in w.statusBar().currentMessage()
        assert w.log_view.minimumHeight() > 0
    finally:
        w.close()
