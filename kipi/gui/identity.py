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
  Wayland also needs a matching .desktop file, so a hidden one is written for the user.

Everything here is best effort: a failure is logged and never stops KiPI starting.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

NAME = "KiPI"
APP_ID = "com.github.drewsum.kipi"        # the plugin.json identifier
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
    import ctypes
    hr = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_ID))
    if hr != 0:
        raise OSError(f"SetCurrentProcessExplicitAppUserModelID failed: {hr:#x}")


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


_IID_PROPERTY_STORE = "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"


def _win_property_store(hwnd: int):
    """IPropertyStore for a window, as (pointer, vtable)."""
    import ctypes
    from ctypes import wintypes

    iid = _guid(_IID_PROPERTY_STORE)
    fn = ctypes.windll.shell32.SHGetPropertyStoreForWindow
    fn.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    fn.restype = ctypes.c_long
    store = ctypes.c_void_p()
    hr = fn(hwnd, ctypes.byref(iid), ctypes.byref(store))
    if hr != 0 or not store:
        raise OSError(f"SHGetPropertyStoreForWindow failed: {hr & 0xFFFFFFFF:#x}")
    vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return store, vtable


def _file_property_store(path: Path, write: bool):
    """IPropertyStore for a file such as a shortcut, as (pointer, vtable)."""
    import ctypes

    ctypes.windll.ole32.CoInitialize(None)      # harmless if COM is up already
    iid = _guid(_IID_PROPERTY_STORE)
    fn = ctypes.windll.shell32.SHGetPropertyStoreFromParsingName
    fn.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                   ctypes.POINTER(ctypes.c_void_p)]
    fn.restype = ctypes.c_long
    store = ctypes.c_void_p()
    hr = fn(str(path), None, 2 if write else 0, ctypes.byref(iid), ctypes.byref(store))
    if hr != 0 or not store:                    # 2: GPS_READWRITE, 0: GPS_DEFAULT
        raise OSError(f"SHGetPropertyStoreFromParsingName failed: {hr & 0xFFFFFFFF:#x}")
    vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return store, vtable


def _set_properties(store, vtable, values) -> None:
    """Set ``(pid, value)`` AppUserModel properties in order, commit and release."""
    import ctypes

    set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_void_p)(vtable[6])
    commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[7])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    keep = []                                   # strings must outlive the SetValue calls
    try:
        for pid, value in values:
            key = _PropertyKey(_guid(_PKEY_FMTID), pid)
            pv = _prop_variant(value, keep)
            hr = set_value(store, ctypes.byref(key), ctypes.byref(pv))
            if hr != 0:
                raise OSError(f"IPropertyStore::SetValue({pid}) failed: {hr & 0xFFFFFFFF:#x}")
        hr = commit(store)
        if hr != 0:
            raise OSError(f"IPropertyStore::Commit failed: {hr & 0xFFFFFFFF:#x}")
    finally:
        release(store)


def _set_windows_relaunch_properties(hwnd: int) -> None:
    ico = RESOURCES / "kipi.ico"
    command = relaunch_command()
    # PreventPinning has to be set before the ID. The display name and icon only count
    # when a relaunch command is set too.
    values = [(_PKEY_PREVENT_PINNING, command is None)]
    if command:
        values += [(_PKEY_RELAUNCH_COMMAND, command), (_PKEY_RELAUNCH_DISPLAY_NAME, NAME)]
        if ico.is_file():
            values.append((_PKEY_RELAUNCH_ICON, f"{ico},0"))
    values.append((_PKEY_ID, APP_ID))
    _set_properties(*_win_property_store(hwnd), values)


def set_shortcut_app_id(path: Path) -> None:
    """Give a Start menu shortcut KiPI's AppUserModelID, so pinning it and the running
    window share one taskbar button. Raises OSError."""
    _set_properties(*_file_property_store(path, write=True), [(_PKEY_ID, APP_ID)])


def read_windows_property(hwnd: int, pid: int):
    """A string (or bool) AppUserModel property of a window; for tests."""
    return _read_property(*_win_property_store(hwnd), pid)


def read_file_property(path: Path, pid: int):
    """A string (or bool) AppUserModel property of a file such as a shortcut; for tests."""
    return _read_property(*_file_property_store(path, write=False), pid)


def _read_property(store, vtable, pid: int):
    import ctypes

    get_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_void_p)(vtable[5])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    try:
        key, pv = _PropertyKey(_guid(_PKEY_FMTID), pid), _PropVariant()
        if get_value(store, ctypes.byref(key), ctypes.byref(pv)) != 0:
            return None
        value = (ctypes.wstring_at(pv.ptr) if pv.vt == _VT_LPWSTR and pv.ptr
                 else ctypes.c_short.from_buffer(pv, 8).value != 0 if pv.vt == _VT_BOOL
                 else None)
        ctypes.windll.ole32.PropVariantClear(ctypes.byref(pv))
        return value
    finally:
        release(store)


_PKEY_FMTID = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"   # PKEY_AppUserModel_*
_PKEY_ID = 5
_PKEY_RELAUNCH_COMMAND = 2
_PKEY_RELAUNCH_ICON = 3
_PKEY_RELAUNCH_DISPLAY_NAME = 4
_PKEY_PREVENT_PINNING = 9
_VT_BOOL, _VT_LPWSTR = 11, 31


def _guid(text: str):
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    g = GUID()
    g.bytes[:] = uuid.UUID(text).bytes_le
    return g


def _ctypes_structs():
    import ctypes

    class PropertyKey(ctypes.Structure):
        _fields_ = [("fmtid", ctypes.c_ubyte * 16), ("pid", ctypes.c_ulong)]

        def __init__(self, guid, pid):
            super().__init__()
            self.fmtid[:] = guid.bytes[:]
            self.pid = pid

    class PropVariant(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                    ("r3", ctypes.c_ushort), ("ptr", ctypes.c_void_p), ("pad", ctypes.c_void_p)]

    return PropertyKey, PropVariant


if sys.platform == "win32":
    _PropertyKey, _PropVariant = _ctypes_structs()


def _prop_variant(value, keep):
    import ctypes

    pv = _PropVariant()
    if isinstance(value, bool):
        pv.vt = _VT_BOOL
        ctypes.c_short.from_buffer(pv, 8).value = -1 if value else 0   # VARIANT_TRUE
    else:
        buf = ctypes.create_unicode_buffer(value)
        keep.append(buf)
        pv.vt, pv.ptr = _VT_LPWSTR, ctypes.addressof(buf)
    return pv


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
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
