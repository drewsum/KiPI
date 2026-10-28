"""The 3D view builds off the UI thread (#209): the solder masks and silkscreens come with
the nets' background build, once per board or stackup."""

import os
import threading
import time

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _idle(app, w, timeout=30.0):
    t0, quiet = time.time(), 0
    while time.time() - t0 < timeout:
        app.processEvents()
        quiet = quiet + 1 if not w._busy() and not w._sync_timer.isActive() else 0
        if quiet >= 10:
            return
        time.sleep(0.01)
    raise AssertionError("window did not go idle")


def test_coatings_are_built_with_the_nets_in_the_background(app, no_3d, monkeypatch):
    from fake_source import FakeSource
    from kipi.core.viz import meshes
    from kipi.gui.main_window import MainWindow
    built = []
    monkeypatch.setattr(meshes, "board_coatings",
                        lambda snap: built.append(threading.current_thread()) or ["mask"])
    w = MainWindow(source=FakeSource())
    shown = []
    w.viewer.set_coatings = shown.append
    _idle(app, w)
    assert built and built[0] is not threading.main_thread()
    assert shown == [["mask"]] and not w._coatings_wanted
    w.sync_nets()                               # every net shown: nothing to build
    _idle(app, w)
    assert len(built) == 1
    st = w.snapshot.stackup
    w._stackup_changed(type(st)(**{**vars(st), "edited": True}))
    _idle(app, w)
    assert len(built) == 2 and len(shown) == 2
    w.close()
