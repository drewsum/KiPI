"""The Stackup window's cross-section (#104): every layer a band to scale (thin ones kept
visible), labelled with its thickness, redrawn as thicknesses are typed."""

import os

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.core.model.stackup import thicknesses  # noqa: E402
from kipi.dc.pi.model import PISetup  # noqa: E402
from kipi.gui import stackup_view as sv  # noqa: E402
from kipi.gui.stackup_window import StackupWindow  # noqa: E402
from phase_boards import board  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def unit_setting(monkeypatch):
    from kipi.gui import stackup_window
    store = {}

    class Settings:
        def __init__(self, *a):
            pass

        def value(self, key, default=None):
            return store.get(key, default)

        def setValue(self, key, value):
            store[key] = value

    monkeypatch.setattr(stackup_window, "QSettings", Settings)
    return store


def test_band_heights_are_to_scale_with_thin_layers_kept_visible():
    mm = [0.01, 1.5, 0.01]
    h = sv.band_heights(mm, 300, 3)
    assert sum(h) == pytest.approx(300)
    assert h[0] == h[2] == 3
    assert h[1] == pytest.approx(294)
    even = sv.band_heights([0.2, 0.4, 0.2], 400, 3)          # all big enough: to scale
    assert even == pytest.approx([100, 200, 100])
    assert sv.band_heights([], 100) == []
    assert sv.band_heights([1, 1, 1], 6, 3) == [2, 2, 2]      # no room: shared evenly


def test_labels_spread_apart_and_stay_inside():
    ys = sv.spread_labels([50, 51, 52, 200], 14, 7, 293)
    assert ys[0] <= 50 and all(b - a >= 14 - 1e-9 for a, b in zip(ys, ys[1:]))
    assert ys[-1] == 200
    ys = sv.spread_labels([280, 290, 299], 14, 7, 293)
    assert ys[-1] == 293 and ys[0] == pytest.approx(265)


def test_dielectric_colors_suggest_the_material():
    fr4 = sv.dielectric_color("FR4")
    assert sv.dielectric_color("Polyimide") != fr4
    assert sv.dielectric_color("Rogers RO4350B") != fr4
    assert sv.dielectric_color("Aluminum nitride") != sv.dielectric_color("Aluminum core")
    assert sv.dielectric_color("") == fr4


def test_window_draws_every_layer_and_follows_edits(app, unit_setting):
    st = board().stackup
    win = StackupWindow()
    win.set_board(st, setup=PISetup(), colors={st.copper[0].id: (1.0, 0.0, 0.0)})
    win.cmb_unit.setCurrentText("µm")
    bands = win.view.bands()
    assert [b.mm for b in bands] == pytest.approx(thicknesses(st))
    assert bands[0].copper and bands[0].color == (1.0, 0.0, 0.0)
    assert bands[0].label == f"{st.copper[0].name}  35.0 µm"
    assert not bands[1].copper
    win.chk_lock.setChecked(False)
    win._thk_spins[0].setValue(70.0)                         # redrawn before the timer
    assert win.view.bands()[0].mm == pytest.approx(0.070)
    assert "70.0 µm" in win.view.bands()[0].label
    win.cmb_unit.setCurrentText("oz")
    assert win.view.bands()[0].label.endswith("2.000 oz")
    assert win.view.bands()[1].label.endswith("mil")         # dielectrics in mil
    win.resize(1150, 600)
    win.view.grab()                                          # paints without error
    win.flush()


def test_clicking_a_layer_focuses_its_thickness(app, unit_setting):
    win = StackupWindow()
    win.set_board(board().stackup, setup=PISetup())
    win.show()
    win.view.grab()
    r = win.view._rects[1]
    assert win.view.band_at(r.center().y()) == 1
    win._focus_row(1)
    assert win.focusWidget() in (win._thk_spins[1], win._thk_spins[1].lineEdit())
    win.close()


def test_no_board_draws_an_empty_view(app, unit_setting):
    win = StackupWindow()
    win.set_board(None)
    assert win.view.bands() == []
    win.view.grab()
