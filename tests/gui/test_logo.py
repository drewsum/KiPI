"""The animated logo plays its full-colour frame strip, scaled for the display (#231)."""

import os

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QLabel  # noqa: E402

from kipi.gui import logo  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_the_frame_strip_ships_with_its_frame_times(app):
    path = logo.frames_path()
    assert path is not None
    p = logo.LogoPlayer(path, QLabel(), 160)
    assert p.frameCount() > 1 and len(p.durations) == p.frameCount()
    assert sum(p.durations) > 1000              # one loop lasts seconds, not a blink


def test_frames_are_scaled_for_the_display_and_rescaled_when_it_changes(app, monkeypatch):
    label = QLabel()
    p = logo.LogoPlayer(logo.frames_path(), label, 96)
    p.start()
    assert label.pixmap().width() == round(96 * p.dpr)
    monkeypatch.setattr(logo, "_dpr", lambda label: 1.5)   # moved to a 150 % screen
    assert p.jumpToFrame(3)
    assert p.dpr == 1.5 and p.currentFrameNumber() == 3
    pm = p.pixmaps[3]            # what the label was given (it may hand back a copy)
    assert pm.width() == 144 and pm.devicePixelRatio() == 1.5    # 96 logical pixels
    p.stop()


def test_it_loops(app):
    p = logo.LogoPlayer(logo.frames_path(), QLabel(), 32)
    seen = []
    p.frameChanged.connect(seen.append)
    p.jumpToFrame(p.frameCount() - 1)
    p._next()
    assert seen == [p.frameCount() - 1, 0]
    p.stop()


def test_an_unreadable_strip_is_not_valid(app, tmp_path):
    bad = tmp_path / "kipi_frames.png"
    bad.write_bytes(b"not a png")
    assert not logo.LogoPlayer(bad, QLabel(), 32).isValid()
