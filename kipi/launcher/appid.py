"""Windows AppUserModel properties: what the taskbar groups a window or a shortcut under,
and what a pinned button runs. Standard library only, as the launcher sets the ID on its
Start menu shortcut before KiPI's dependencies are installed; :mod:`kipi.gui.identity`
sets them on KiPI's window."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

APP_ID = "com.github.drewsum.kipi"        # the plugin.json identifier


_IID_PROPERTY_STORE = "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"


def win_property_store(hwnd: int):
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


def set_properties(store, vtable, values) -> None:
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


def set_shortcut_app_id(path: Path) -> None:
    """Give a Start menu shortcut KiPI's AppUserModelID, so pinning it and the running
    window share one taskbar button. Raises OSError."""
    set_properties(*_file_property_store(path, write=True), [(PKEY_ID, APP_ID)])


def set_process_app_id() -> None:
    """Group this process's windows under KiPI's taskbar button, with the window icon,
    instead of Python's. Only works before the first window opens. Raises OSError."""
    import ctypes
    hr = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_ID))
    if hr != 0:
        raise OSError(f"SetCurrentProcessExplicitAppUserModelID failed: {hr & 0xFFFFFFFF:#x}")


def tag_window(hwnd: int, command: Optional[str], name: str, icon: Optional[Path]) -> None:
    """Name a window's taskbar button and say what pinning it runs (``command``; None turns
    pinning off). Raises OSError."""
    # PreventPinning has to be set before the ID. The display name and icon only count
    # when a relaunch command is set too.
    values = [(PKEY_PREVENT_PINNING, command is None)]
    if command:
        values += [(PKEY_RELAUNCH_COMMAND, command), (PKEY_RELAUNCH_DISPLAY_NAME, name)]
        if icon is not None and icon.is_file():
            values.append((PKEY_RELAUNCH_ICON, f"{icon},0"))
    values.append((PKEY_ID, APP_ID))
    set_properties(*win_property_store(hwnd), values)


def read_windows_property(hwnd: int, pid: int):
    """A string (or bool) AppUserModel property of a window; for tests."""
    return _read_property(*win_property_store(hwnd), pid)


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
PKEY_ID = 5
PKEY_RELAUNCH_COMMAND = 2
PKEY_RELAUNCH_ICON = 3
PKEY_RELAUNCH_DISPLAY_NAME = 4
PKEY_PREVENT_PINNING = 9
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

