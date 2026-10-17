"""Qt application bootstrap shared by the KiCad plugin entry and the standalone CLI."""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

LOG_DIR = Path(tempfile.gettempdir()) / "kipi"
LOG_PATH = LOG_DIR / "kipi.log"          # replaced per session by setup_logging()
KEEP_SESSIONS = 15
STARTED = time.monotonic()      # this module is the first of KiPI's an entry point loads

log = logging.getLogger("kipi")
_crash_file = None   # kept open for faulthandler for the life of the process


def setup_logging(level=logging.INFO, console: bool = None) -> Path:
    """Write this session's log to %TEMP%/kipi/kipi-<time>-<pid>.log.

    Inside KiCad the plugin has no console, so these files are the only record of
    what went wrong. Each session gets its own log and its own crash/hang file
    (``*.crash.log``), so one run never overwrites another; the newest
    KEEP_SESSIONS sessions are kept.
    """
    global _crash_file, LOG_PATH
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    LOG_PATH = LOG_DIR / f"kipi-{stamp}-{os.getpid()}.log"
    crash_path = LOG_PATH.with_suffix(".crash.log")
    _prune_old_logs()
    handlers = [logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")]
    # Only echo to an interactive terminal. When KiCad launches the plugin, stderr is a
    # pipe KiCad does not always drain; once its few-KB buffer fills, a write blocks
    # forever and freezes whichever thread was logging (a solve, or the UI).
    if console is None:
        console = bool(sys.stderr) and _isatty(sys.stderr)
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(threadName)s: "
                        "%(message)s", handlers=handlers, force=True)
    try:
        _crash_file = open(crash_path, "w", encoding="utf-8")
        _crash_file.write("# KiPI native crash / hang stack dumps for this session.\n"
                          "# 'Windows fatal exception: code 0x8001010d' entries are harmless COM\n"
                          "# notices that Windows raises and Python reports; they are not crashes.\n")
        _crash_file.flush()
        # Current thread only: on Windows faulthandler also fires on first-chance COM
        # exceptions (0x8001010d) raised inside Qt's message loop, and walking the stacks
        # of threads that are still running (a board load in the worker) can itself
        # access-violate, killing the process with 0xC000041D. A real fault still dumps
        # the faulting thread; the hang watchdog below still dumps all threads.
        faulthandler.enable(file=_crash_file, all_threads=False)
    except OSError:
        pass
    _install_excepthooks()
    log.info("KiPI session log %s (Python %s)", LOG_PATH, sys.version.split()[0])
    return LOG_PATH


def _isatty(stream) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def detach_std_streams() -> Path:
    """Point stdout/stderr, including the OS-level file descriptors that native code
    (VTK, Qt) writes to, at this session's ``.console.log`` instead of whatever pipe the
    parent process gave us. Call after :func:`setup_logging` when running as a KiCad
    plugin, so nothing can ever block writing to KiCad's pipe."""
    console_path = LOG_PATH.with_suffix(".console.log")
    f = open(console_path, "a", buffering=1, encoding="utf-8", errors="replace")
    for fd in (1, 2):
        try:
            os.dup2(f.fileno(), fd)
        except OSError:
            pass
    sys.stdout = sys.stderr = f
    return console_path


def _prune_old_logs():
    try:
        logs = sorted(LOG_DIR.glob("kipi-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        sessions = [p for p in logs
                    if not p.name.endswith((".crash.log", ".console.log"))]
        for old in sessions[KEEP_SESSIONS:]:
            old.unlink(missing_ok=True)
            old.with_suffix(".crash.log").unlink(missing_ok=True)
            old.with_suffix(".console.log").unlink(missing_ok=True)
    except OSError:
        pass


_watchdog_stop: Optional[threading.Event] = None
_ui_beat: Optional[float] = None     # time.monotonic() of the UI thread's last heartbeat


def ui_alive():
    """Heartbeat from the UI thread (a QTimer), so the watchdog can tell when it stalls."""
    global _ui_beat
    _ui_beat = time.monotonic()


def watchdog_start(seconds: float = 60.0, native_seconds: float = 300.0,
                   ui_stall_seconds: float = 3.0):
    """Dump every thread's stack to the crash log every ``seconds`` until stopped.

    Called when a background task starts, so a task that never finishes leaves a
    record of exactly where it is stuck. The dumps come from a Python thread holding
    the GIL (``sys._current_frames``): faulthandler's own timer walks the other
    threads' frames without the GIL, and doing that every minute to a solve that is
    running (not stuck) can access-violate and kill the process. faulthandler stays
    as a dead man's switch, re-armed by every Python dump, so it only fires when no
    Python code has run for ``native_seconds`` (a thread stuck in native code with
    the GIL held, whose frames are then not changing).

    While the task runs, a UI thread that has not sent :func:`ui_alive` for
    ``ui_stall_seconds`` (Windows calls the window "Not Responding" after 5 s) is
    logged once per stall, with the UI thread's stack.
    """
    global _watchdog_stop
    watchdog_stop()
    if _crash_file is None:
        return
    stop = threading.Event()

    def run():
        t0 = time.perf_counter()
        next_dump = seconds
        ui_reported = False
        while not stop.wait(min(1.0, seconds)):
            if _ui_beat is not None:
                lag = time.monotonic() - _ui_beat
                if lag > ui_stall_seconds and not ui_reported:
                    ui_reported = True
                    stack = _python_stacks().get(threading.main_thread().ident)
                    stack = "".join(stack.format()) if stack else "(none)\n"
                    log.warning("UI thread has not responded for %.1f s; it is at:\n%s",
                                lag, stack)
                elif lag <= ui_stall_seconds and ui_reported:
                    ui_reported = False
                    log.info("UI thread responding again")
            elapsed = time.perf_counter() - t0
            if elapsed >= next_dump:
                next_dump += seconds
                _dump_python_stacks(elapsed)
                faulthandler.dump_traceback_later(native_seconds, file=_crash_file)

    faulthandler.dump_traceback_later(native_seconds, file=_crash_file)
    threading.Thread(target=run, name="kipi-watchdog", daemon=True).start()
    _watchdog_stop = stop


def watchdog_stop():
    global _watchdog_stop
    if _watchdog_stop is not None:
        _watchdog_stop.set()
        _watchdog_stop = None
    faulthandler.cancel_dump_traceback_later()


def _python_stacks() -> dict:
    """Every thread's Python stack, by thread ident, taken in one go.

    A frame's callers are only found by walking ``f_back``, and a thread that runs on
    meanwhile can leave the frame it was in: a finished generator's frame no longer knows
    its caller, so formatting the stacks as they are walked could lose the functions that
    matter. The frames are read with thread switches held off, then formatted.
    """
    old = sys.getswitchinterval()
    sys.setswitchinterval(max(old, 1.0))
    try:
        out = {}
        for ident, frame in sys._current_frames().items():
            entries = []
            while frame is not None:
                entries.append((frame.f_code.co_filename, frame.f_lineno,
                                frame.f_code.co_name, None))
                frame = frame.f_back
            out[ident] = entries[::-1]
    finally:
        sys.setswitchinterval(old)
    return {ident: traceback.StackSummary.from_list(e) for ident, e in out.items()}


def _dump_python_stacks(elapsed: float):
    names = {t.ident: t.name for t in threading.enumerate()}
    me = threading.get_ident()
    lines = [f"Task still running after {elapsed:.0f} s ({time.strftime('%H:%M:%S')}):\n"]
    for ident, stack in _python_stacks().items():
        if ident != me:
            lines.append(f"Thread {names.get(ident, ident)} (most recent call last):\n")
            lines += stack.format()
    try:
        _crash_file.write("".join(lines) + "\n")
        _crash_file.flush()
    except (OSError, ValueError):
        pass


def _install_excepthooks():
    def report(exc_type, exc, tb, where="UI thread"):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.critical("Uncaught exception in %s:\n%s", where, text)
        if where != "UI thread":
            return
        try:
            from qtpy.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is not None:
                QMessageBox.critical(None, "KiPI error",
                                     f"{exc_type.__name__}: {exc}\n\n"
                                     f"Details were written to {LOG_PATH}")
        except Exception:  # noqa: BLE001 - never let error reporting raise
            pass

    sys.excepthook = report
    threading.excepthook = lambda a: report(a.exc_type, a.exc_value, a.exc_traceback,
                                            f"thread {a.thread.name if a.thread else '?'}")


def _refocus(app, win) -> None:
    from qtpy.QtCore import Qt
    from kipi.gui.single_instance import bring_to_front
    if app.applicationState() == Qt.ApplicationActive and not win.isActiveWindow():
        bring_to_front(win)


def start_splash():
    """The QApplication, and the welcome screen shown on it (None if it cannot be), held
    open until :func:`run` has opened the main window. Shown before anything slow, so
    KiPI answers the click at once (#55)."""
    os.environ.setdefault("QT_API", "pyside6")
    from kipi.gui.identity import make_app
    from kipi.gui.splash import show_splash

    app = make_app()
    app.setStyle("Fusion")
    splash = show_splash(app)
    if splash is not None:
        splash.hold()
    return app, splash


def set_status(splash, text: str) -> None:
    """Say on the welcome screen what KiPI is doing."""
    log.info("Starting: %s", text)
    if splash is not None:
        try:
            splash.set_status(text)
            from qtpy.QtWidgets import QApplication
            QApplication.processEvents()
        except RuntimeError:      # closed by a click; its widgets are gone
            pass


# What the main window needs, slowest first, with what the welcome screen says meanwhile.
# The first start after a reboot or after KiCad starts reads these thousands of files
# from a cold disk (and past a virus scanner), which can take tens of seconds (#55).
_PRELOAD = (
    ("Loading NumPy and SciPy…", ("numpy", "scipy.sparse", "scipy.sparse.linalg",
                                  "scipy.sparse.csgraph", "scipy.spatial",
                                  "scipy.interpolate", "shapely")),
    ("Loading the 3D viewer…", ("pyvista", "pyvista.plotting", "matplotlib.pyplot")),
    ("Loading the solvers…", ("kipi.core.mesh.mesher", "kipi.core.solver.spd",
                              "kipi.dc.solver.setup", "kipi.dc.thermal.model",
                              "kipi.dc.viz.results", "kipi.ac.solver.fem", "kipi.ac.checks")),
)


def preload(splash=None, preload=_PRELOAD) -> None:
    """Import the main window's packages on a thread, keeping the welcome screen painted
    and animated (and Windows from calling KiPI not responding) meanwhile. None of them
    touch Qt. A package that fails here is left for the main window's own import to
    report, on the UI thread as before."""
    import importlib
    from qtpy.QtWidgets import QApplication

    stage = [0]

    def load():
        for i, (_, modules) in enumerate(preload):
            stage[0] = i
            for m in modules:
                try:
                    importlib.import_module(m)
                except Exception:  # noqa: BLE001 - imported again where it is used
                    log.debug("Preloading %s failed", m, exc_info=True)

    t0 = time.perf_counter()
    set_status(splash, preload[0][0])
    thread = threading.Thread(target=load, name="kipi-preload", daemon=True)
    thread.start()
    shown = 0
    while thread.is_alive():
        if stage[0] != shown:
            shown = stage[0]
            set_status(splash, preload[shown][0])
        QApplication.processEvents()
        thread.join(0.03)
    log.info("Loaded KiPI's packages in %.1f s", time.perf_counter() - t0)


def run(source=None, tool=None, snapshot=None, instance=None, splash=False) -> int:
    """Show the main window until it is closed. ``source`` and ``tool`` are the window's
    :class:`~kipi.sources.base.BoardSource` and :class:`~kipi.sources.base.InstalledTool`;
    ``instance`` is this KiPI's :class:`~kipi.gui.single_instance.InstanceGuard`, through
    which a KiPI started later brings this window to the front. ``splash`` is the welcome
    screen from :func:`start_splash` if one was shown already."""
    if splash is False:
        app, splash = start_splash()
    else:
        from kipi.gui.identity import make_app
        app = make_app()
    preload(splash)
    set_status(splash, "Opening the window…")
    from kipi.gui.identity import tag_window
    from kipi.gui.main_window import MainWindow
    from kipi.gui.single_instance import bring_to_front

    win = MainWindow(source=source, tool=tool, snapshot=snapshot)
    bring_to_front(win)     # shows it; it opens behind KiCad otherwise (#112)
    tag_window(win)
    log.info("Window open %.1f s after KiPI started", time.monotonic() - STARTED)
    if splash is not None and splash.isVisible():
        splash.set_status("Ready")
        splash.raise_()     # over the new window until its timer closes it
        # Closing the splash hands the focus back to whatever is under it; keep it in
        # KiPI unless the user went elsewhere meanwhile.
        splash.closed.connect(lambda: _refocus(app, win))
        splash.release()    # closes it now if it has played through, as KiPI was slow
    if instance is not None:
        instance.listen(lambda: bring_to_front(win))
    code = app.exec()
    if instance is not None:
        instance.release()
    task = getattr(win, "_task", None)
    if task is not None and task.isRunning():
        # A worker stuck in native code (e.g. a long direct solve) cannot be interrupted;
        # don't leave a zombie process behind KiCad.
        log.warning("Closing with a background task still running; forcing exit")
        logging.shutdown()
        os._exit(code)
    log.info("KiPI closed normally")
    return code
