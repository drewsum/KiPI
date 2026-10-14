"""The plugin must never block writing to a pipe its parent (KiCad) does not drain."""

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_with_unread_pipes(code: str, tmp_path: Path, timeout: float = 60.0) -> str:
    script = tmp_path / "child.py"
    status = tmp_path / "status.txt"
    script.write_text(textwrap.dedent(code), encoding="utf-8")
    # Session logs go under tmp_path: in the real %TEMP%\kipi they would count toward
    # KEEP_SESSIONS and push the user's own sessions out.
    env = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path))
    # stdout/stderr are pipes that nobody reads, like KiCad's when it is busy.
    p = subprocess.Popen([sys.executable, str(script), str(status)], cwd=ROOT, env=env,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    t0 = time.time()
    while p.poll() is None and time.time() - t0 < timeout:
        time.sleep(0.2)
    hung = p.poll() is None
    if hung:
        p.kill()
    assert not hung, "child blocked writing to an undrained pipe"
    if not status.exists():
        err = p.stderr.read().decode(errors="replace")[-2000:]
        raise AssertionError(f"child failed (exit {p.returncode}): {err}")
    return status.read_text()


def test_logging_does_not_echo_into_a_pipe(tmp_path):
    out = _run_with_unread_pipes("""
        import sys, logging
        sys.path.insert(0, ".")
        from kipi.gui import app
        app.setup_logging()                      # stderr is a pipe, not a terminal
        for i in range(20000):
            logging.getLogger("kipi").info("progress line %d %s", i, "x" * 80)
        echo = [h for h in logging.getLogger().handlers if type(h) is logging.StreamHandler]
        open(sys.argv[1], "w").write(f"done {len(echo)}")
    """, tmp_path)
    assert out == "done 0"


def test_detached_streams_absorb_native_and_python_writes(tmp_path):
    out = _run_with_unread_pipes("""
        import os, sys, logging
        sys.path.insert(0, ".")
        from kipi.gui import app
        app.setup_logging(console=True)          # even with console echo forced on
        path = app.detach_std_streams()
        for i in range(5000):
            logging.getLogger("kipi").info("line %d %s", i, "y" * 80)
            print("print", i)
            os.write(2, b"native stderr" + bytes([10]))
            os.write(1, b"native stdout" + bytes([10]))
        open(sys.argv[1], "w").write("done " + str(os.path.getsize(path) > 100_000))
    """, tmp_path)
    assert out == "done True"


def test_watchdog_dumps_a_running_task_from_python(tmp_path):
    out = _run_with_unread_pipes("""
        import sys, threading, time
        sys.path.insert(0, ".")
        from kipi.gui import app
        log = app.setup_logging()
        stop = threading.Event()
        def busy_solver_step():
            while not stop.is_set():
                sum(i * i for i in range(1000))   # keeps changing frames, like a solve
        t = threading.Thread(target=busy_solver_step, name="worker")
        t.start()
        app.watchdog_start(seconds=0.2)
        time.sleep(1.0)
        app.watchdog_stop()
        stop.set()
        t.join()
        text = log.with_suffix(".crash.log").read_text()
        open(sys.argv[1], "w").write(f"{text.count('Task still running')} "
                                     f"{'busy_solver_step' in text} {'Timeout' in text}")
    """, tmp_path)
    n, has_worker, native = out.split()
    assert int(n) >= 2 and has_worker == "True" and native == "False"


def test_watchdog_reports_a_stalled_ui_thread_with_its_stack(tmp_path):
    out = _run_with_unread_pipes("""
        import sys, time
        sys.path.insert(0, ".")
        from kipi.gui import app
        log = app.setup_logging()
        app.ui_alive()
        app.watchdog_start(seconds=0.2, ui_stall_seconds=0.5)
        def slow_slot():
            time.sleep(1.5)              # the UI thread stuck in one call
        slow_slot()
        for _ in range(10):              # back to the QTimer's regular heartbeat
            app.ui_alive()
            time.sleep(0.05)
        app.watchdog_stop()
        text = log.read_text()
        open(sys.argv[1], "w").write(f"{text.count('UI thread has not responded')} "
                                     f"{'slow_slot' in text} {'responding again' in text}")
    """, tmp_path)
    assert out == "1 True True"
