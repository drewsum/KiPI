"""The welcome screen shows the animated logo and version, and closes on its own."""

import os
import time

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QLabel  # noqa: E402

from kipi import __version__  # noqa: E402
from kipi.gui import logo, splash  # noqa: E402


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


def test_shows_version_and_packages(app, plugin_host):
    s = splash.Splash()
    text = _text(s)
    assert __version__ in text and "com.github.drewsum.kipi" in text and "Python" in text
    assert "KiCad plugin" in text
    assert "Drew Maatman (drewmaatman@gmail.com)" in text      # the author
    s.close()


def test_standalone_says_so_and_is_no_plugin(app):
    s = splash.Splash()
    text = _text(s)
    assert "Standalone" in text and "com.github.drewsum.kipi" not in text
    assert __version__ in text
    s.close()


def test_logo_is_animated(app):
    s = splash.Splash()
    assert isinstance(s.movie, logo.LogoPlayer) and s.movie.frameCount() > 1
    assert s.movie.label.pixmap().width() == splash.LOGO_SIZE * s.movie.dpr
    s.close()


def test_logo_falls_back_to_the_gif_without_the_frame_strip(app, monkeypatch):
    monkeypatch.setattr(logo, "frames_path", lambda: None)
    s = splash.Splash()
    assert isinstance(s.movie, logo.QMovie) and s.movie.frameCount() > 1
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


def _no_animation(monkeypatch):
    monkeypatch.setattr(logo, "frames_path", lambda: None)
    monkeypatch.setattr(logo.QMovie, "isValid", lambda self: False)


def test_still_logo_closes_after_a_timer(app, monkeypatch):
    _no_animation(monkeypatch)
    s = splash.show_splash(app, show_ms=200)
    assert s is not None and s.movie is None and s.isVisible()
    _wait_closed(app, s)
    assert not s.isVisible()


def test_falls_back_to_the_still_logo_without_gif_support(app, monkeypatch):
    _no_animation(monkeypatch)
    s = splash.Splash()
    assert s.movie is None
    assert any(lb.pixmap() is not None and not lb.pixmap().isNull()
               for lb in s.findChildren(QLabel))


def test_a_broken_splash_does_not_stop_startup(app, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no screen")
    monkeypatch.setattr(splash, "Splash", boom)
    assert splash.show_splash(app) is None


def test_held_until_the_window_is_open_however_long_starting_takes(app):
    s = splash.show_splash(app, show_ms=100)
    s.hold()
    s.set_status("Loading the 3D viewer…")
    assert "Loading the 3D viewer…" in _text(s)
    s.movie.jumpToFrame(s.movie.frameCount() - 1)   # one loop played while loading
    _spin(app, 0.5)
    assert s.isVisible()
    s.release()
    _wait_closed(app, s)
    assert not s.isVisible()


def test_released_early_it_still_plays_its_loop(app):
    s = splash.show_splash(app, show_ms=100)
    s.hold()
    s.release()
    _spin(app, 0.3)
    assert s.isVisible()
    s.movie.jumpToFrame(s.movie.frameCount() - 1)
    _wait_closed(app, s)
    assert not s.isVisible()


def test_packages_load_on_a_thread_while_the_splash_says_what(app, monkeypatch):
    """#55: the slow imports of a cold start happen behind the welcome screen, which
    keeps painting; one that fails is left for the main window to report."""
    import threading
    from kipi.gui import app as gui_app

    said, threads = [], []
    real = __import__("importlib").import_module

    def fake_import(name, *a):
        threads.append(threading.current_thread().name)
        if name == "no_such_package_kipi":
            raise ImportError(name)
        return real(name, *a)

    monkeypatch.setattr("importlib.import_module", fake_import)
    s = splash.Splash()
    monkeypatch.setattr(s, "set_status", said.append)
    gui_app.preload(s, (("One…", ("json", "no_such_package_kipi")), ("Two…", ("csv",))))
    assert said[0] == "One…" and set(said) <= {"One…", "Two…"}
    assert threads and set(threads) == {"kipi-preload"}
    s.close()
