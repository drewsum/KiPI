"""Only one KiPI runs at a time; a second one brings the first to the front."""

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.gui import single_instance as si  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """A lock and socket of this test's own, so a KiPI the developer has open is untouched."""
    name = f"kipi-test-{os.getpid()}-{tmp_path.name}"
    monkeypatch.setattr(si, "server_name", lambda: name)
    monkeypatch.setattr(si, "lock_path", lambda: tmp_path / "kipi.lock")
    return name


def test_second_instance_finds_the_lock_taken(isolated):
    first = si.acquire()
    assert first is not None and first.locked
    assert si.acquire() is None
    assert si.running_pid() == os.getpid()
    first.release()
    again = si.acquire()
    assert again is not None and again.locked
    again.release()


def test_a_dead_owner_does_not_lock_the_next_kipi_out(isolated, tmp_path):
    # A KiPI that crashed: its lock file names a process that no longer exists.
    p = subprocess.run([sys.executable, "-c", textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, ".")
        from kipi.gui import single_instance as si
        g = si.acquire(__import__("pathlib").Path(r"{tmp_path / 'kipi.lock'}"))
        assert g is not None and g.locked
        os._exit(0)                       # never unlocks
    """)], cwd=ROOT, env=dict(os.environ, QT_API="pyside6"), timeout=60)
    assert p.returncode == 0 and (tmp_path / "kipi.lock").exists()
    g = si.acquire()
    assert g is not None and g.locked
    g.release()


def test_an_unwritable_lock_still_lets_kipi_start(monkeypatch, tmp_path):
    g = si.acquire(tmp_path / "no such folder" / "kipi.lock")
    assert g is not None and not g.locked
    assert g.listen(lambda: None) is False
    g.release()


def test_second_instance_brings_the_first_to_the_front(app, isolated):
    first = si.acquire()
    activated = []
    assert first.listen(lambda: activated.append(True))
    # The second KiPI is another process, as it is when KiCad starts it again.
    child = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import sys
        sys.path.insert(0, ".")
        from kipi.gui import single_instance as si
        si.server_name = lambda: "{isolated}"
        sys.exit(0 if si.activate_running(timeout_ms=10000) else 3)
    """)], cwd=ROOT, env=dict(os.environ, QT_API="pyside6"))
    deadline = time.monotonic() + 60
    while child.poll() is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    first.release()
    assert child.returncode == 0 and activated == [True]


def test_activation_fails_cleanly_with_nobody_listening(app, isolated):
    assert si.activate_running(timeout_ms=500) is False
