"""Shared test fixtures."""

import pytest


@pytest.fixture
def no_3d(monkeypatch):
    """Build MainWindow with a stand-in for the 3D view.

    Qt's offscreen platform cannot create an OpenGL widget on Linux, so the real view has
    no interactor there (CI). Tests of the window's own logic do not need it.
    """
    from qtpy.QtWidgets import QWidget
    from kipi.gui import main_window

    class StubViewer(QWidget):
        result = None
        zscale = 1.0

        def __init__(self, parent=None):
            super().__init__(parent)
            self._nets = []

        def nets(self):
            return list(self._nets)

        def add_net(self, ns, render=True):
            if ns.net not in self._nets:
                self._nets.append(ns.net)

        def remove_net(self, net, render=True):
            if net in self._nets:
                self._nets.remove(net)

        def render(self, *a, **k):            # QWidget has its own render()
            pass

        def __getattr__(self, name):          # every other view call is a no-op
            return lambda *a, **k: None

    monkeypatch.setattr(main_window, "Viewer3D", StubViewer)
