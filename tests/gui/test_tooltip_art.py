"""Drawings in tooltips (#116): SVG templates in kipi/gui/graphics, coloured from the
palette, rendered once and shown under the tooltip text, or the text alone without them."""

import os
import re

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtGui import QColor, QImage, QPalette  # noqa: E402
from qtpy.QtWidgets import QApplication, QWidget  # noqa: E402

from kipi.gui import tooltip_art as ta  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    """Render into a folder of this test's own, without the drawings already made."""
    monkeypatch.setattr(ta.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ta, "_rendered", {})


def test_the_graphics_folder_holds_the_drawings():
    assert ta.GRAPHICS.is_dir()
    assert {"cap_model", "dc_bias", "aging", "vrm_types", "load_line", "dropout", "accuracy",
            "bandwidth", "phases", "remote_sense", "target_types", "series_link", "mesh",
            "current_split", "tolerance", "vrm_pins", "dielectric"} <= set(ta.names())


@pytest.mark.parametrize("name", ta.names())
def test_each_drawing_renders_in_the_palette(app, name):
    svg = (ta.GRAPHICS / f"{name}.svg").read_text(encoding="utf-8")
    assert '"{bg}"' in svg                       # on the GUI's background
    shown = ta.image(name)
    assert shown is not None, name
    path, w, h = shown
    img = QImage(str(path))
    assert (img.width(), img.height()) == (w * ta.SCALE, h * ta.SCALE)
    bg = QColor(ta.palette_colors()["bg"])
    assert img.pixelColor(w, 4 * ta.SCALE).name() == bg.name()   # top edge, inside the corner


def test_fill_colours_every_token():
    colors = {"bg": "#111111", "fg": "#eeeeee", "muted": "#888888", "grid": "#222222",
              "accent": "#ff8800", "warn": "#663322"}
    out = ta.fill('<a f="{bg}"/><b f="{c}"/><c f="{l}"/>', colors, ["l"])
    assert out == '<a f="#111111"/><b f="#eeeeee"/><c f="#ff8800"/>'
    for name in ta.names():
        svg = ta.fill((ta.GRAPHICS / f"{name}.svg").read_text(encoding="utf-8"), colors)
        assert not re.search(r"\{\w+\}", svg), name


def test_accent_stands_out_even_where_the_highlight_is_grey(app):
    pal = QPalette(QColor(40, 40, 40))
    pal.setColor(QPalette.Highlight, QColor(70, 70, 70))
    assert ta.accent_color(pal).hsvSaturation() > 90               # the orange instead
    pal.setColor(QPalette.Highlight, QColor(48, 140, 198))
    assert ta.accent_color(pal) == QColor(48, 140, 198)


def test_a_drawing_follows_the_palette(app):
    dark = QPalette(QColor(40, 40, 40))
    light = QPalette(QColor(240, 240, 240))
    assert ta.image("cap_model", (), dark)[0] != ta.image("cap_model", (), light)[0]
    assert ta.image("cap_model", ("l",))[0] != ta.image("cap_model", ("r",))[0]


def test_art_tip_puts_the_drawing_under_the_text(app):
    tip = ta.art_tip("ESL & ESR\nof <one> part", "cap_model", "l")
    assert "ESL &amp; ESR<br>of &lt;one&gt; part" in tip
    assert re.search(r'<img src="file:[^"]+cap_model-\w+\.png"', tip)


def test_without_svg_support_the_tooltip_is_its_text(app, monkeypatch):
    monkeypatch.setattr(ta, "_render_png", lambda svg, path: None)
    assert ta.art_tip("Load line", "load_line", "ll") == "Load line"
    assert ta.art_tip("Unknown", "no_such_drawing") == "Unknown"


def test_every_drawing_shows_in_a_tooltip(app, no_3d):
    """Each drawing in the folder is used, so the folder holds no strays."""
    pytest.importorskip("pyvistaqt")
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    tips = " ".join(c.toolTip() for c in w.findChildren(QWidget))
    # Or shown in a window, as the Tolerance window's diagram is.
    tips += " ".join(f"/{c.property('art')}-" for c in w.findChildren(QWidget)
                     if c.property("art"))
    w.close()
    for name in ta.names():
        assert f"/{name}-" in tips, name


def test_add_text_goes_above_the_drawing(app):
    assert ta.add_text("", "Why") == "Why"
    assert ta.add_text("Plain", "Why") == "Plain\n\nWhy"
    tip = ta.add_text(ta.art_tip("Load line", "load_line", "ll"), "Used by <DC>")
    assert tip.index("Used by &lt;DC&gt;") < tip.index("<img")


def test_pin_pickers_show_their_drawing_once_under_all_their_text(app, no_3d):
    """Each box of a pin picker shows the row's text, the box's own line and any
    'used by' note above one drawing, in the DC and AC VRM editors alike (#116)."""
    pytest.importorskip("pyvistaqt")
    from kipi.dc.gui.pi_panel import LinkEditor, VrmEditor
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    vrms = w.findChildren(VrmEditor)
    links = w.findChildren(LinkEditor)
    assert {e.mode for e in vrms} == {"dc", "ac"} and links
    for e in vrms:
        e._kind_rows()                       # as on showing a VRM: grays the other analysis
        for picker, part in ((e.output, "out"), (e.input, "inp"), (e.ground, "gnd")):
            for box in (picker, picker.cmb_net, picker.cmb_part, picker.txt_pins):
                t = box.toolTip()
                assert t.count("<img") == 1 and "/vrm_pins-" in t, (e.mode, part)
            t = picker.txt_pins.toolTip()
            assert t.index("Pins: pin numbers") < t.index("<img")
            assert "&lt;table" not in t
        note = e.input.cmb_net.toolTip()
        if e.mode == "ac":
            assert note.index("Used by the DC analysis only") < note.index("<img")
    for e in links:
        assert "/series_link-" in e.a.cmb_part.toolTip()
    w.close()
