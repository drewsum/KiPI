"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def no_kicad_settings(monkeypatch):
    """Tests never read this machine's KiCad color theme."""
    from kipi.sources.kicad_ipc import colors
    monkeypatch.setattr(colors, "_config_roots", lambda: [])


@pytest.fixture(autouse=True)
def standalone_host(monkeypatch):
    """Tests run standalone unless they say otherwise (``kipi.host.set_current``), whatever
    this machine's environment, and a host one test sets never leaks into the next."""
    from kipi import host
    monkeypatch.delenv(host.HOST_ENV, raising=False)
    monkeypatch.delenv(host.KICAD_SOCKET_ENV, raising=False)
    monkeypatch.setattr(host, "_current", None)


@pytest.fixture
def plugin_host(monkeypatch):
    """Run the test as the KiCad plugin."""
    from kipi import host
    monkeypatch.setattr(host, "_current", host.Host.PLUGIN)


@pytest.fixture(autouse=True)
def opened_in_browser(monkeypatch):
    """Tests never open a real web browser: what would be opened is recorded here."""
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda uri, *a, **k: opened.append(uri) or True)
    try:
        from qtpy.QtGui import QDesktopServices
    except Exception:                       # no Qt binding: nothing else can open one
        return opened
    monkeypatch.setattr(QDesktopServices, "openUrl",
                        lambda url: opened.append(url.toLocalFile()) or True)
    return opened


@pytest.fixture
def no_3d(monkeypatch):
    """Build MainWindow with a stand-in for the 3D view.

    Qt's offscreen platform cannot create an OpenGL widget on Linux, so the real view has
    no interactor there (CI). Tests of the window's own logic do not need it.
    """
    from qtpy.QtCore import Signal
    from qtpy.QtWidgets import QWidget
    from kipi.gui import main_window

    class StubViewer(QWidget):
        result = None
        zscale = 1.0
        result_shown = Signal(bool)

        def __init__(self, parent=None):
            super().__init__(parent)
            self._nets = []
            self.models_shown = self.models_wanted = True
            self.shown = {}

        def set_feature_visible(self, feature, visible):
            if feature == "models":
                self.models_shown = self.models_wanted = visible
            self.shown[feature] = visible

        def feature_shown(self, feature):
            return self.shown.get(feature, False)

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

        pi_markers = ()

        def set_pi_markers(self, markers):
            self.pi_markers = list(markers)

        def __getattr__(self, name):          # every other view call is a no-op
            return lambda *a, **k: None

    monkeypatch.setattr(main_window, "Viewer3D", StubViewer)
