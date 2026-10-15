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


def test_logo_is_animated(app):
    s = splash.Splash()
    assert s.movie is not None and s.movie.frameCount() > 1
    assert any(lb.movie() is s.movie for lb in s.findChildren(QLabel))
    s.close()


def _wait_closed(app, s, seconds=10):
    deadline = time.monotonic() + seconds
    while s.isVisible() and time.monotonic() < deadline:
        _spin(app, 0.05)


def test_closes_itself_after_one_loop_of_the_logo(app):
    s = splash.show_splash(app, show_ms=200)
    assert s is not None and s.isVisible()
    _spin(app, 0.5)
    assert s.isVisible()            # the short timer is not what closes it
    s.movie.jumpToFrame(s.movie.frameCount() - 1)   # skip to the end of the loop
    _wait_closed(app, s)
    assert not s.isVisible()


def test_still_logo_closes_after_a_timer(app, monkeypatch):
    monkeypatch.setattr(splash.QMovie, "isValid", lambda self: False)
    s = splash.show_splash(app, show_ms=200)
    assert s is not None and s.movie is None and s.isVisible()
    _wait_closed(app, s)
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
