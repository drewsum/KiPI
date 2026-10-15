"""Allow one KiPI at a time per user.

The first KiPI holds a lock file and listens on a local socket (a named pipe on Windows).
A second one finds the lock taken, asks the first to bring its window to the front, and
exits; if the first does not answer, it says that KiPI is already open instead.

The lock is a :class:`QLockFile`, which records the owner's PID, so a KiPI that crashed
or was killed does not lock the next one out. Like the app identity, this is best effort:
if the lock file cannot be written at all, KiPI starts anyway.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

os.environ.setdefault("QT_API", "pyside6")

from qtpy.QtCore import QLockFile  # noqa: E402

log = logging.getLogger(__name__)

ACTIVATE = b"activate\n"
ACK = b"ok\n"
TIMEOUT_MS = 2000


def _user() -> str:
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name (e.g. a container)
        name = ""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "user"


def lock_path() -> Path:
    return Path(tempfile.gettempdir()) / f"kipi-{_user()}.lock"


def server_name() -> str:
    return f"kipi-{_user()}"


class InstanceGuard:
    """Held by the KiPI that is running; released when it closes."""

    def __init__(self, lock: Optional[QLockFile]):
        self._lock = lock
        self._server = None

    @property
    def locked(self) -> bool:
        return self._lock is not None

    def listen(self, on_activate: Callable[[], None]) -> bool:
        """Answer later KiPIs by calling ``on_activate`` (needs the QApplication)."""
        if self._lock is None:
            return False
        try:
            from qtpy.QtNetwork import QLocalServer
        except ImportError:
            log.info("QtNetwork is not available; a second KiPI will only be told to close")
            return False
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.UserAccessOption)
        # We hold the lock, so a socket left by a KiPI that crashed is stale (Unix only).
        QLocalServer.removeServer(server_name())
        if not server.listen(server_name()):
            log.warning("Could not listen for other KiPI instances: %s", server.errorString())
            return False

        def accept():
            while server.hasPendingConnections():
                sock = server.nextPendingConnection()
                sock.disconnected.connect(sock.deleteLater)
                sock.readyRead.connect(lambda s=sock: answer(s))

        def answer(sock):
            if bytes(sock.readAll()).startswith(ACTIVATE.strip()):
                log.info("Another KiPI was started; bringing this one to the front")
                try:
                    on_activate()
                finally:
                    sock.write(ACK)
                    sock.flush()
                    sock.disconnectFromServer()

        server.newConnection.connect(accept)
        self._server = server
        return True

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None


def acquire(path: Optional[Path] = None) -> Optional[InstanceGuard]:
    """The guard if this is the only KiPI, or None if another one holds the lock."""
    lock = QLockFile(str(path or lock_path()))
    lock.setStaleLockTime(0)        # only a dead owner makes the lock stale, never its age
    if lock.tryLock(0):
        return InstanceGuard(lock)
    if lock.error() == QLockFile.LockFailedError:
        return None
    # Not a running KiPI: the temp folder is read-only, full, or similar. Run unguarded.
    log.warning("Could not create the single-instance lock %s (error %s); "
                "not checking for another KiPI", path or lock_path(), lock.error())
    return InstanceGuard(None)


def running_pid(path: Optional[Path] = None) -> Optional[int]:
    try:
        pid, _host, _app = QLockFile(str(path or lock_path())).getLockInfo()
        return int(pid) or None
    except Exception:  # noqa: BLE001 - older bindings return a different tuple
        return None


def activate_running(timeout_ms: int = TIMEOUT_MS) -> bool:
    """Ask the running KiPI to come to the front; True if it said it did."""
    try:
        from qtpy.QtNetwork import QLocalSocket
    except ImportError:
        return False
    if sys.platform == "win32":
        # Windows only lets the foreground process hand focus on. KiCad started us from a
        # click, so we may; pass that right to the running KiPI.
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)    # ASFW_ANY
        except Exception:  # noqa: BLE001
            pass
    sock = QLocalSocket()
    sock.connectToServer(server_name())
    if not sock.waitForConnected(timeout_ms):
        log.info("The running KiPI did not answer: %s", sock.errorString())
        return False
    sock.write(ACTIVATE)
    sock.flush()
    ok = sock.waitForBytesWritten(timeout_ms) and sock.waitForReadyRead(timeout_ms)
    ok = ok and bytes(sock.readAll()).startswith(ACK.strip())
    sock.abort()
    return ok


def bring_to_front(win) -> None:
    """Show ``win`` above other windows, restoring it if minimized."""
    from qtpy.QtCore import Qt
    if win.isMinimized():
        win.showNormal()
    win.setWindowState((win.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
    win.show()
    win.raise_()
    win.activateWindow()


def claim(path: Optional[Path] = None) -> Optional[InstanceGuard]:
    """The guard for this KiPI, or None after handing over to one already running."""
    guard = acquire(path)
    if guard is not None:
        return guard
    pid = running_pid(path)
    log.info("KiPI is already running (PID %s)", pid)
    if activate_running():
        return None
    from qtpy.QtWidgets import QMessageBox
    from kipi.gui.identity import make_app
    _app = make_app()  # noqa: F841 - must outlive the box
    QMessageBox.information(
        None, "KiPI",
        "KiPI is already open" + (f" (PID {pid})" if pid else "") + ".\n\n"
        "Only one KiPI can run at a time. Switch to its window, or close it and start "
        "KiPI again.")
    return None
