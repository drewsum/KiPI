"""Make KiPI show as itself, not as "Python", in the taskbar, Dock or panel.

KiCad starts the plugin with its own Python, so by default the operating system shows that
interpreter's name and icon. Each platform needs its own fix:

* Windows: an explicit AppUserModelID gives KiPI its own taskbar button with the window
  icon, and per-window properties name it "KiPI" and say what a pinned button runs: the
  standalone launcher, since nothing outside KiCad can start the plugin. Without the
  launcher, pinning is turned off. The launcher's Start menu shortcut carries the same
  ID, so a pin from the Start menu and the running window share one button.
* macOS: the app name in the menu bar comes from the main bundle's CFBundleName, set
  before Qt starts; the Dock icon comes from the application icon.
* Linux: the window icon and WM_CLASS / Wayland app id ("kipi") name the window; GNOME on
  Wayland also needs a matching .desktop file, so a hidden one is written for the user,
  unless the launcher's menu entry is there already.

Everything here is best effort: a failure is logged and never stops KiPI starting.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

from kipi.launcher.appid import APP_ID, set_process_app_id  # noqa: F401 - tests use APP_ID
from kipi.launcher.appid import tag_window as tag_windows_button

log = logging.getLogger(__name__)

NAME = "KiPI"
DESKTOP_NAME = "kipi"
ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "resources"


def icon_files() -> List[Path]:
    """The icon files that ship with the plugin, smallest first."""
    names = ["kipi.ico", "kipi_24.png", "kipi_48.png", "kipi_256.png"]
    return [RESOURCES / n for n in names if (RESOURCES / n).is_file()]


def make_app(argv: Optional[List[str]] = None):
    """The QApplication, named and given the KiPI icon (creating it if needed)."""
    from qtpy.QtWidgets import QApplication

    _before_app()
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setApplicationName(NAME)
    app.setDesktopFileName(DESKTOP_NAME)
    icon = app_icon()
    if icon is not None:
        app.setWindowIcon(icon)
    if sys.platform.startswith("linux") and app.platformName() in ("xcb", "wayland"):
        _try(_write_desktop_file)
    return app


def app_icon():
    from qtpy.QtGui import QIcon

    files = icon_files()
    if not files:
        return None
    icon = QIcon()
    for f in files:
        icon.addFile(str(f))
    return icon


def tag_window(win) -> None:
    """Per-window identity, once the window exists (Windows only)."""
    if sys.platform == "win32":
        _try(_set_windows_relaunch_properties, int(win.winId()))


def _before_app() -> None:
    """Settings that only work before the QApplication exists."""
    if sys.platform == "win32":
        _try(_set_windows_app_id)
    elif sys.platform == "darwin":
        _try(_set_macos_bundle_name)
    elif sys.platform.startswith("linux"):
        # X11 WM_CLASS is (RESOURCE_NAME or argv[0], application name), else "python3".
        os.environ.setdefault("RESOURCE_NAME", DESKTOP_NAME)


def _try(fn, *args) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 - cosmetic; never stop KiPI starting
        log.debug("Could not set the app identity with %s", fn.__name__, exc_info=True)


# --- Windows --------------------------------------------------------------------------

def _set_windows_app_id() -> None:
    set_process_app_id()


def relaunch_command() -> Optional[str]:
    """What a pinned taskbar button runs: the standalone launcher, with no console, by
    the Python that started it (else this one). None without the launcher."""
    launcher = ROOT / "kipi_launcher.pyw"
    if not launcher.is_file():
        return None
    python = Path(os.environ.get("KIPI_LAUNCHER_PYTHON") or sys.executable)
    if python.with_name("pythonw.exe").is_file():
        python = python.with_name("pythonw.exe")
    return f'"{python}" "{launcher}"'


def _set_windows_relaunch_properties(hwnd: int) -> None:
    tag_windows_button(hwnd, relaunch_command(), NAME, RESOURCES / "kipi.ico")


# --- macOS ----------------------------------------------------------------------------

def _set_macos_bundle_name() -> None:
    """Rename the running app (menu bar, Dock, app switcher) from "Python" to KiPI."""
    import ctypes
    import ctypes.util

    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    ctypes.cdll.LoadLibrary(ctypes.util.find_library("Foundation"))
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    send_addr = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value

    def send(obj, sel, *args, restype=ctypes.c_void_p, argtypes=()):
        fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(send_addr)
        return fn(obj, objc.sel_registerName(sel.encode()), *args)

    def nsstring(text):
        return send(objc.objc_getClass(b"NSString"), "stringWithUTF8String:", text.encode(),
                    argtypes=(ctypes.c_char_p,))

    bundle = send(objc.objc_getClass(b"NSBundle"), "mainBundle")
    mutable = objc.objc_getClass(b"NSMutableDictionary")
    for getter in ("localizedInfoDictionary", "infoDictionary"):
        info = send(bundle, getter)
        if info and send(info, "isKindOfClass:", mutable, restype=ctypes.c_bool,
                         argtypes=(ctypes.c_void_p,)):
            for key in ("CFBundleName", "CFBundleDisplayName"):
                send(info, "setObject:forKey:", nsstring(NAME), nsstring(key),
                     restype=None, argtypes=(ctypes.c_void_p, ctypes.c_void_p))


# --- Linux ----------------------------------------------------------------------------

def desktop_entry() -> str:
    icon = RESOURCES / "kipi_256.png"
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={NAME}",
        "Comment=DC power integrity analysis for KiCad",
        f"Icon={icon}",
        "Exec=false",                     # KiPI starts from KiCad, not from a menu
        "NoDisplay=true",
        f"StartupWMClass={NAME}",
        "",
    ])


def _write_desktop_file() -> None:
    """Hidden ~/.local/share/applications/kipi.desktop, so GNOME on Wayland can match the
    window's app id to a name and icon. Rewritten only when it would change."""
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = base / "applications" / f"{DESKTOP_NAME}.desktop"
    text = desktop_entry()
    if path.is_file():
        old = path.read_text(encoding="utf-8")
        # The launcher's menu entry (--create-shortcut) serves the window just as well.
        if old == text or "kipi_launcher" in old:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
