"""The Log names what draws the 3D view, and warns when that is not a graphics card
(#205)."""

import logging
import os

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.gui.viewer3d import software_renderer  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("name, software", [
    ("NVIDIA GeForce RTX 4070/PCIe/SSE2", False),
    ("Intel(R) UHD Graphics 620", False),
    ("AMD Radeon RX 7800 XT", False),
    ("Apple M2", False),
    ("GDI Generic", True),                                   # Windows, no driver / RDP
    ("llvmpipe (LLVM 15.0.7, 256 bits)", True),              # Mesa
    ("Google SwiftShader", True),
    ("Microsoft Basic Render Driver", True),
])
def test_software_renderers_are_told_apart(name, software):
    assert software_renderer(name) is software


@pytest.mark.parametrize("name, level", [("NVIDIA GeForce RTX 4070/PCIe/SSE2", logging.INFO),
                                         ("GDI Generic", logging.WARNING)])
def test_the_renderer_is_logged_once(app, no_3d, monkeypatch, name, level):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    monkeypatch.setattr(w.viewer, "graphics", lambda: name, raising=False)
    said = []
    monkeypatch.setattr(w, "log", lambda msg, lvl=logging.INFO: said.append((msg, lvl)))
    w._log_graphics()
    w._log_graphics()
    assert len(said) == 1 and name in said[0][0] and said[0][1] == level
    w.close()


def test_an_unknown_renderer_is_not_logged(app, no_3d, monkeypatch):
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    monkeypatch.setattr(w.viewer, "graphics", lambda: "", raising=False)
    said = []
    monkeypatch.setattr(w, "log", lambda *a, **k: said.append(a))
    w._log_graphics()
    assert said == []
    w.close()
