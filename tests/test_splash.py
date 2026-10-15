"""The welcome screen shows the animated logo and version, and closes on its own."""

import os
import time

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QLabel  # noqa: E402

from kipi import __version__  # noqa: E402
from kipi.gui import splash  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _spin(app, seconds):
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        app.processEvents()
        time.sleep(0.01)


def _text(s):
    return " ".join(lb.text() for lb in s.findChildren(QLabel))


def test_shows_version_and_packages(app):
    s = splash.Splash()
    text = _text(s)
    assert __version__ in text and "com.github.drewsum.kipi" in text and "Python" in text
    s.close()


def test_logo_is_animated_past_its_opening_hold(app):
    s = splash.Splash()
    assert s.movie is not None and s.movie.frameCount() > 1
    assert s.movie.currentFrameNumber() >= 1
    assert any(lb.movie() is s.movie for lb in s.findChildren(QLabel))
    s.close()


def test_closes_itself_without_input(app):
    s = splash.show_splash(app, show_ms=200)
    assert s is not None and s.isVisible()
    deadline = time.monotonic() + 5
    while s.isVisible() and time.monotonic() < deadline:
        _spin(app, 0.05)
    assert not s.isVisible()


def test_falls_back_to_the_still_logo_without_gif_support(app, monkeypatch):
    monkeypatch.setattr(splash.QMovie, "isValid", lambda self: False)
    s = splash.Splash()
    assert s.movie is None
    assert any(lb.pixmap() is not None and not lb.pixmap().isNull()
               for lb in s.findChildren(QLabel))


def test_a_broken_splash_does_not_stop_startup(app, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no screen")
    monkeypatch.setattr(splash, "Splash", boom)
    assert splash.show_splash(app) is None
