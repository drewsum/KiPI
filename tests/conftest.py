# Portions Copyright (c) 2026 Tomachie LLC.
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


@pytest.fixture(autouse=True)
def step_timings(monkeypatch, tmp_path_factory):
    """Tests keep their solve times out of this machine's history, and each starts with
    none (#224)."""
    import uuid
    from kipi.core import eta
    history = eta.History(tmp_path_factory.getbasetemp() / f"timings-{uuid.uuid4().hex}.json")
    monkeypatch.setattr(eta, "HISTORY", history)
    return history


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
    from qtpy.QtWidgets import QMessageBox, QWidget
    from kipi.gui import main_window

    # A window closed with setup edits not saved asks whether to save them; no one is
    # there to answer, so they are dropped (a test of the question patches it itself).
    ask = QMessageBox.question
    monkeypatch.setattr(QMessageBox, "question", lambda parent, title, text, *a, **k:
                        QMessageBox.Discard if "not saved" in text
                        else ask(parent, title, text, *a, **k))

    class StubViewer(QWidget):
        result = None
        zscale = 1.0
        result_shown = Signal(bool)

        def __init__(self, parent=None):
            super().__init__(parent)
            self._nets = []
            self.included_nets = set()
            self.hide_non_included = False
            self.models_shown = self.models_wanted = True
            self.shown = {}

        def set_included_nets(self, nets):
            self.included_nets = set(nets)

        def set_hide_non_included(self, hidden):
            self.hide_non_included = bool(hidden)

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
        coupling = None
        hot_loops = None
        fields = None

        def set_coupling(self, overlay):
            self.coupling = overlay

        def set_hot_loops(self, overlay):
            self.hot_loops = overlay

        def set_fields(self, overlay):
            self.fields = overlay

        def set_pi_markers(self, markers):
            self.pi_markers = list(markers)

        def __getattr__(self, name):          # every other view call is a no-op
            return lambda *a, **k: None

    monkeypatch.setattr(main_window, "Viewer3D", StubViewer)
