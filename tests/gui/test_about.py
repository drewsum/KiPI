"""The About dialog opens from the toolbar and names the version, repo and icon."""

import json
import os

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication, QLabel  # noqa: E402

from kipi import __version__  # noqa: E402
from kipi.gui import about  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_icon_ships_with_the_plugin():
    assert about.icon_path() is not None


def test_dialog_names_the_version_and_repo(app):
    d = about.AboutDialog()
    text = " ".join(lb.text() for lb in d.findChildren(QLabel))
    assert __version__ in text and about.REPO in text



def test_dialog_icon_is_animated(app):
    assert about.animated_icon_path() is not None
    d = about.AboutDialog()
    assert d.movie is not None and d.movie.frameCount() > 1
    assert any(lb.movie() is d.movie for lb in d.findChildren(QLabel))


def test_dialog_falls_back_to_the_still_icon_without_gif_support(app, monkeypatch):
    # As on a Qt build without the GIF image plugin: QMovie cannot read the file.
    monkeypatch.setattr(about.QMovie, "isValid", lambda self: False)
    d = about.AboutDialog()
    assert d.movie is None
    assert any(lb.pixmap() is not None and not lb.pixmap().isNull()
               for lb in d.findChildren(QLabel))


def test_toolbar_has_help_and_about_after_open_results(app, no_3d):
    from qtpy.QtWidgets import QToolBar
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    names = [a.text() for a in w.findChild(QToolBar).actions() if a.text()]
    assert names[-3:] == ["Open results…", "Help", "About"]
    w.close()


# Repo statistics (#59). The network and the settings store are stubbed for every test.

STATS = {"schema": 1, "updated": "2026-10-15T03:17:00Z",
         "clones": {"total": 1234, "since": "2026-10-01", "uniques_14d": 231, "daily": {}},
         "release_downloads": {"total": 56, "by_release": {"v0.7.0": 56}}}

_real_fetch_stats = about.fetch_stats


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    from qtpy.QtCore import QSettings
    ini = str(tmp_path / "kipi.ini")
    monkeypatch.setattr(about, "_settings", lambda: QSettings(ini, QSettings.IniFormat))
    monkeypatch.setattr(about, "fetch_stats", lambda *a, **k: None)
    monkeypatch.setattr(about, "_fetched_this_session", False)


def _wait(cond, ms=2000):
    from qtpy.QtCore import QDeadlineTimer, QThread
    deadline = QDeadlineTimer(ms)
    while not cond() and not deadline.hasExpired():
        QApplication.processEvents()
        QThread.msleep(10)
    return cond()


def test_parse_stats_reads_what_the_workflow_writes():
    s = about.parse_stats(STATS)
    assert s == about.RepoStats(1234, "2026-10-01", 231, 56)
    assert s.text() == ("1,234 clones since 2026-10-01 (231 unique in the last 14 days) "
                        "· 56 release downloads")


@pytest.mark.parametrize("bad", [None, [], {}, {"clones": {}}, {"clones": {"total": "x"}}, "text"])
def test_parse_stats_rejects_anything_else(bad):
    assert about.parse_stats(bad) is None


def test_fetch_stats_reads_a_url_and_fails_quietly(tmp_path):
    good = tmp_path / "stats.json"
    good.write_text(json.dumps(STATS))
    assert _real_fetch_stats(good.as_uri()) == STATS
    assert _real_fetch_stats((tmp_path / "missing.json").as_uri()) is None
    good.write_text("not json")
    assert _real_fetch_stats(good.as_uri()) is None
    good.write_text('{"something": "else"}')
    assert _real_fetch_stats(good.as_uri()) is None


def test_stats_line_is_hidden_when_offline_with_nothing_cached(app):
    d = about.AboutDialog()
    assert not d.stats_label.isVisibleTo(d)


def test_stats_line_shows_fetched_stats_and_caches_them(app, monkeypatch):
    monkeypatch.setattr(about, "fetch_stats", lambda *a, **k: STATS)
    d = about.AboutDialog()
    assert _wait(lambda: d.stats_label.isVisibleTo(d))
    assert "1,234 clones" in d.stats_label.text()
    assert about.cached_stats() == about.parse_stats(STATS)


def test_stats_line_falls_back_to_the_cache_when_offline(app):
    about._cache_stats(STATS)
    d = about.AboutDialog()
    assert d.stats_label.isVisibleTo(d) and "56 release downloads" in d.stats_label.text()


def test_stats_are_fetched_once_per_session(app, monkeypatch):
    calls = []
    monkeypatch.setattr(about, "fetch_stats", lambda *a, **k: calls.append(1) or STATS)
    d1 = about.AboutDialog()
    assert _wait(lambda: d1.stats_label.isVisibleTo(d1))
    d2 = about.AboutDialog()
    assert d2._fetch is None and len(calls) == 1
    assert d2.stats_label.isVisibleTo(d2)    # from the cache the first fetch filled
