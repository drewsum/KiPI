"""Board sources and installed tools: what the window asks of wherever a board comes from,
and how the running KiCad and the installed KiCad answer."""

import os
import time

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.sources.base import Capabilities, InstalledTool  # noqa: E402
from synthetic import make_board  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


from fake_source import FakeSource  # noqa: E402,F401 - also used by test_host


def _idle(app, w, timeout=30.0):
    """Wait until the window stays idle: a task's thread stops before its result reaches
    the window, and the result can start the next task."""
    t0, quiet = time.time(), 0
    while time.time() - t0 < timeout:
        app.processEvents()
        quiet = quiet + 1 if not w._busy() and not w._sync_timer.isActive() else 0
        if quiet >= 10:
            return
        time.sleep(0.01)
    raise AssertionError("window did not go idle")


# --- The window --------------------------------------------------------------------------

def test_a_source_gets_load_refill_and_select(app, no_3d, plugin_host):
    from kipi.gui.main_window import MainWindow
    src = FakeSource()
    w = MainWindow(source=src)
    _idle(app, w)
    assert src.loads == [False]                          # read once at start
    assert w.snapshot is not None
    assert w.act_reload.isVisible() and w.act_reload.text() == "Load from Fake"
    assert w._act_refill.isVisible()
    assert not w.btn_select.isHidden() and w.btn_select.text() == "Select in Fake"

    w.chk_refill.setChecked(True)
    w.reload_board()
    _idle(app, w)
    assert src.loads == [False, True]

    w._picked = ("pad", "1234")
    w._select_in_source()
    assert src.selected == [("pad", "1234")]
    w.close()


def test_a_source_that_cannot_refill_hides_it(app, no_3d):
    from kipi.gui.main_window import MainWindow

    class NoRefill(FakeSource):
        caps = Capabilities(reload=True)

    src = NoRefill()
    w = MainWindow(source=src)
    _idle(app, w)
    w.chk_refill.setChecked(True)                        # hidden, but even if ticked
    w.reload_board()
    _idle(app, w)
    assert src.loads == [False, False]
    assert not w._act_refill.isVisible() and w.btn_select.isHidden()
    w.close()


def test_without_a_source_only_snapshots_open(app, no_3d):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    assert not w.act_reload.isVisible() and not w._act_refill.isVisible()
    assert w.btn_select.isHidden()
    w.reload_board()                                     # nothing to read from
    assert w._task is None
    w.close()


def test_without_a_tool_colors_are_kipis_and_there_are_no_models():
    from kipi.core.viz.layer_colors import DEFAULT_COPPER
    tool = InstalledTool()
    colors, where = tool.layer_colors(make_board())
    assert colors == DEFAULT_COPPER and "KiPI" in where
    assert tool.part_models(make_board()) == (None, "")


# --- KiCad -------------------------------------------------------------------------------

def test_the_live_source_reads_the_board_and_its_version(monkeypatch):
    pytest.importorskip("kipy")
    from kicad_fake import FakeKiCad
    from kipi.sources.kicad_ipc import extract
    from kipi.sources.kicad_ipc.source import KiCadLiveSource

    class Kicad:
        def get_version(self):
            return "10.0.6"

    fake = FakeKiCad("10.0.6")
    connected = []

    def connect(progress=None):
        connected.append(True)
        return Kicad(), fake.board()

    monkeypatch.setattr(extract, "require_single_kicad", lambda: None)
    monkeypatch.setattr(extract, "connect", connect)
    src = KiCadLiveSource()
    assert not src.can_select()                          # not connected yet
    snap = src.load()
    assert connected == [True] and src.can_select()
    assert snap.kicad_version == "10.0.6" and snap.tracks
    src.load()
    assert connected == [True]                           # connects once


def test_the_live_source_selects_by_kiid():
    pytest.importorskip("kipy")
    from kipi.sources.kicad_ipc.source import KiCadLiveSource

    class Board:
        def __init__(self):
            self.calls = []

        def clear_selection(self):
            self.calls.append("clear")

        def add_to_selection(self, items):
            self.calls.append([i.id.value for i in items])

    board = Board()
    KiCadLiveSource(board=board).select("via", "abc-123")
    assert board.calls == ["clear", ["abc-123"]]


def test_the_live_source_centres_the_view_on_the_item():
    # #208: no call moves the view to a point; the item is selected and centred on.
    pytest.importorskip("kipy")
    from kipy.proto.common.commands.editor_commands_pb2 import RunActionStatus
    from kipi.sources.kicad_ipc.source import CENTRE_ACTIONS, KiCadLiveSource

    class Board:
        def __init__(self):
            self.calls = []

        def clear_selection(self):
            self.calls.append("clear")

        def add_to_selection(self, items):
            self.calls.append([i.id.value for i in items])

        def set_active_layer(self, layer):
            self.calls.append(("layer", layer))

    class Kicad:
        def __init__(self, known):
            self.known, self.ran = known, []

        def run_action(self, action):
            self.ran.append(action)
            ok = action in self.known
            return type("R", (), {"status": RunActionStatus.RAS_OK if ok
                                  else RunActionStatus.RAS_INVALID})()

    board, kicad = Board(), Kicad(set(CENTRE_ACTIONS))
    KiCadLiveSource(kicad, board).focus("track", "t-1", 4)
    assert board.calls == ["clear", ["t-1"], ("layer", 4)]
    assert kicad.ran == ["common.Control.centerSelection"]
    kicad = Kicad({"common.Control.zoomFitSelection"})      # a KiCad without centring
    KiCadLiveSource(kicad, Board()).focus("via", "v-1")
    assert kicad.ran == list(CENTRE_ACTIONS)

    class NoLayer(Board):
        def set_active_layer(self, layer):
            raise RuntimeError("not a board layer")

    kicad = Kicad(set(CENTRE_ACTIONS))
    KiCadLiveSource(kicad, NoLayer()).focus("pad", "p-1", 99)
    assert kicad.ran == ["common.Control.centerSelection"]    # the view still moves


def test_installed_kicad_says_why_there_are_no_models(monkeypatch):
    from kipi.sources.kicad_ipc import models3d
    from kipi.sources.kicad_ipc.source import InstalledKiCad, KiCadCliModels, KiCadLiveSource

    asked = []
    monkeypatch.setattr(models3d, "find_kicad_cli",
                        lambda kicad=None: asked.append(kicad) or None)
    tool = InstalledKiCad()
    provider, why = tool.part_models(make_board())
    assert provider is None and "kicad-cli" in why and models3d.CLI_ENV in why
    tool.part_models(make_board())
    assert asked == [None]                               # looked for once

    kicad = object()
    monkeypatch.setattr(models3d, "find_kicad_cli",
                        lambda kicad=None: asked.append(kicad) or ["kicad-cli"])
    monkeypatch.setattr(models3d, "board_file", lambda snap: None)
    tool = InstalledKiCad(KiCadLiveSource(kicad=kicad))
    provider, why = tool.part_models(make_board())
    assert asked[-1] is kicad                            # the running KiCad's own kicad-cli
    assert provider is None and "board file" in why

    monkeypatch.setattr(models3d, "board_file", lambda snap: "b.kicad_pcb")
    loaded = []
    monkeypatch.setattr(models3d, "load_models",
                        lambda cli, board, refs, progress=None: loaded.append((cli, board))
                        or {r: None for r in refs})
    provider, why = tool.part_models(make_board())
    assert isinstance(provider, KiCadCliModels) and why == ""
    assert provider.load(["U1"]) == {"U1": None}
    assert loaded == [(["kicad-cli"], "b.kicad_pcb")]
