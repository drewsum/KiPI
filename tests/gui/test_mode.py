"""The analysis mode switch at the right end of the toolbar: DC by default, AC shows its
own (placeholder) setup panel and keeps the DC setup for when DC comes back."""

import os

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QLabel, QRadioButton, QToolBar  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, no_3d):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    yield w
    w.close()


def _mode(w, key) -> QRadioButton:
    return w.findChild(QRadioButton, f"mode_{key}")


def test_mode_buttons_are_last_on_the_toolbar(window):
    tb = window.findChild(QToolBar)
    widgets = [tb.widgetForAction(a) for a in tb.actions()]
    tail = widgets[-3:]
    assert isinstance(tail[0], QLabel) and tail[0].text().strip() == "Mode:"
    assert [rb.text() for rb in tail[1:]] == ["DC", "AC"]


def test_dc_is_the_default(window):
    assert window.mode == "dc"
    assert _mode(window, "dc").isChecked() and not _mode(window, "ac").isChecked()
    assert window.setup_stack.currentWidget() is window.solve_panel
    assert window.solve_dock.windowTitle() == "PI setup"


def test_ac_swaps_the_setup_panel_and_dc_brings_it_back(window):
    setup = window.solve_panel.setup
    _mode(window, "ac").setChecked(True)
    assert window.mode == "ac" and not _mode(window, "dc").isChecked()
    assert window.setup_stack.currentWidget() is window.ac_panel
    assert window.solve_dock.windowTitle() == "AC setup"
    assert "AC" in window.act_open_results.toolTip()       # opens AC results here
    _mode(window, "dc").setChecked(True)
    assert window.mode == "dc"
    assert window.setup_stack.currentWidget() is window.solve_panel
    assert window.solve_panel.setup is setup          # the DC setup was kept
    assert "DC" in window.act_open_results.toolTip()


def test_mode_does_not_change_while_a_task_runs(window, monkeypatch):
    from qtpy.QtWidgets import QMessageBox
    monkeypatch.setattr(window, "_busy", lambda: True)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    _mode(window, "ac").setChecked(True)
    assert window.mode == "dc"
    assert _mode(window, "dc").isChecked() and not _mode(window, "ac").isChecked()
    assert window.setup_stack.currentWidget() is window.solve_panel


def _plot_tab_shown(w) -> bool:
    i = w.views.indexOf(w.plot)
    return w.views.isTabVisible(i) if hasattr(w.views, "isTabVisible") else w.views.isTabEnabled(i)


def test_impedance_plot_is_a_tab_beside_the_3d_view_in_ac_only(window):
    assert window.views.indexOf(window.viewer) == 0 and window.views.indexOf(window.plot) == 1
    assert not _plot_tab_shown(window)
    _mode(window, "ac").setChecked(True)
    assert _plot_tab_shown(window)
    window.views.setCurrentWidget(window.plot)
    _mode(window, "dc").setChecked(True)
    assert not _plot_tab_shown(window)
    assert window.views.currentWidget() is window.viewer
