"""Signs of life while KiPI works (#111): the status bar spinner with the time taken, the
moving progress bar between stages and the wait cursor while results are drawn."""

import os
import time

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import Qt  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.gui import heartbeat as hb  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_elapsed_text():
    assert hb.elapsed_text(7.9) == "0:07"
    assert hb.elapsed_text(102) == "1:42"
    assert hb.elapsed_text(3723) == "1:02:03"


def test_spinner_shows_the_newest_activity_and_hides_when_all_stop(app, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(hb.time, "monotonic", lambda: now[0])
    beat = hb.Heartbeat()
    beat.start("task", "Solving power tree")
    beat._tick()
    assert beat.isHidden()                     # quick operations never show it
    now[0] += 65
    beat._tick()
    assert not beat.isHidden() and beat.label.text() == "Solving power tree · 1:05"
    angle = beat.spinner.angle
    beat._tick()
    assert beat.spinner.angle != angle         # it turns
    beat.start("models", "Loading 3D models")
    now[0] += 3
    beat._tick()
    assert beat.label.text() == "Loading 3D models · 0:03"
    beat.stop("models")
    assert beat.active and beat.label.text() == "Solving power tree · 1:08"
    beat.stop("task")
    assert not beat.active and beat.isHidden() and not beat._timer.isActive()


def test_spinner_paints(app):
    spinner = hb._Spinner()
    spinner.grab()                              # paintEvent runs without error


def test_busy_cursor_is_restored_after_an_error(app):
    with pytest.raises(RuntimeError):
        with hb.busy_cursor():
            assert QApplication.overrideCursor().shape() == Qt.WaitCursor
            raise RuntimeError
    assert QApplication.overrideCursor() is None


@pytest.fixture
def window(app, no_3d):
    pytest.importorskip("pyvistaqt")
    from kipi.gui.main_window import MainWindow
    w = MainWindow()
    yield w
    w.close()


def test_a_task_beats_while_it_runs_and_draws_under_the_wait_cursor(window):
    cursors = []
    done = []

    def work(progress):
        time.sleep(0.05)
        return 42

    def on_done(result):
        cursors.append(QApplication.overrideCursor().shape())
        done.append(result)

    task = window._run(work, on_done, "Test task")
    assert window.heartbeat.active and window.act_cancel.isEnabled()
    assert window.progress_bar.maximum() == 0           # moving from the start
    task.wait(5000)
    for _ in range(50):
        QApplication.processEvents()
        if not window.heartbeat.active:
            break
        time.sleep(0.01)
    assert done == [42] and cursors == [Qt.WaitCursor]
    assert not window.heartbeat.active and not window.act_cancel.isEnabled()
    assert QApplication.overrideCursor() is None


def test_each_stage_moves_until_it_reports(window):
    window.progress_bar.setRange(0, 1000)
    window.progress_bar.setValue(1000)                   # the last stage, finished
    window._on_stage("Factorizing")
    assert window.progress_bar.maximum() == 0
    window._last_fraction_t = 0.0
    window._on_fraction(0.5)
    assert window.progress_bar.maximum() == 1000 and window.progress_bar.value() == 500
