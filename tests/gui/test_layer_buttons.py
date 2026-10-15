"""The copper layer list's All and None buttons show or hide every layer."""

import os

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import Qt  # noqa: E402
from qtpy.QtWidgets import QApplication, QListWidgetItem, QPushButton  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_all_and_none_set_every_layer(app, no_3d):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    shown = {}
    w.viewer.set_layer_visible = lambda lid, on: shown.__setitem__(lid, on)
    for lid in ("F.Cu", "In1.Cu", "B.Cu"):
        it = QListWidgetItem(lid)
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(Qt.Checked)
        it.setData(Qt.UserRole, lid)
        w.layer_list.addItem(it)
    buttons = {b.text(): b for b in w.layer_list.parent().findChildren(QPushButton)}
    buttons["None"].click()
    assert shown == {"F.Cu": False, "In1.Cu": False, "B.Cu": False}
    w.layer_list.item(1).setCheckState(Qt.Checked)
    shown.clear()
    buttons["All"].click()
    assert shown == {"F.Cu": True, "B.Cu": True}          # In1.Cu was already shown
    w.close()
