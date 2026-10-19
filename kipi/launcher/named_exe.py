"""Make KiPI show as "KiPI" with its icon in Windows Task Manager (#124), not as "Python".

Task Manager names an app by the version information of the program that owns its window,
and draws that program's icon. KiPI runs on a venv's ``pythonw.exe``, a small launcher
that starts the base interpreter's ``pythonw.exe``, which owns the window: "Python".

So KiPI makes ``KiPI.exe`` in the venv's Scripts folder: a copy of the base interpreter
with KiPI's icon and a version resource that says "KiPI". The Python DLLs it loads are
copied beside it, since Windows looks for them beside the program. Python finds the
venv's pyvenv.cfg one folder up, so it runs with the venv's packages, exactly as the
launcher would. :func:`relaunch` starts KiPI again on it.

The copy is made once, and again when the base Python or KiPI's icon or version changes.
A stamp file beside it records what it was made from. A copy that fails to start is
recorded in the stamp and not tried again until those change. Anything that goes wrong
leaves KiPI running on ``pythonw.exe`` as before.

Standard library only: the standalone launcher uses it before KiPI's packages are installed.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence

NAME = "KiPI"
EXE_NAME = "KiPI.exe"
STAMP_NAME = "KiPI.exe.json"
RELAUNCHED_ENV = "KIPI_NAMED_EXE"         # the file the copy writes once it starts
ROOT = Path(__file__).resolve().parents[2]
ICON = ROOT / "resources" / "kipi.ico"
STARTUP_WAIT_S = 15.0                     # for the copy to say it started (cold disk)

RT_ICON, RT_GROUP_ICON, RT_VERSION = 3, 14, 16
LANG_EN_US = 0x0409
CODEPAGE_UNICODE = 1200


def _version() -> str:
    try:
        from kipi import __version__
        return __version__
    except Exception:  # noqa: BLE001 - a version resource without it is fine
        return "0.0.0"


# --- resources ------------------------------------------------------------------------

def _pad4(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % 4)


def _node(key: str, value: bytes = b"", text: bool = False, children: Sequence[bytes] = ()):
    """One block of a VS_VERSIONINFO tree: wLength, wValueLength, wType, the key, the
    value and the children, each 32-bit aligned."""
    body = _pad4(b"\0" * 6 + (key + "\0").encode("utf-16-le"))
    body += value
    if children:
        body = _pad4(body) + b"".join(_pad4(c) for c in children)
    value_len = len(value) // 2 if text else len(value)
    return struct.pack("<HHH", len(body), value_len, 1 if text else 0) + body[6:]


def _string(key: str, value: str) -> bytes:
    return _node(key, (value + "\0").encode("utf-16-le"), text=True)


def version_resource(description: str = NAME, version: str = "") -> bytes:
    """A VS_VERSIONINFO naming the program ``description``, as Task Manager shows it."""
    version = version or _version()
    nums = [int(p) for p in (version.split("+")[0].split(".") + ["0"] * 4)[:4] if p.isdigit()]
    nums = (nums + [0] * 4)[:4]
    ms, ls = (nums[0] << 16) | nums[1], (nums[2] << 16) | nums[3]
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x00010000, ms, ls, ms, ls, 0x3F, 0,
                        0x00040004, 1, 0, 0, 0)     # VOS_NT_WINDOWS32, VFT_APP
    table = _node(f"{LANG_EN_US:04x}{CODEPAGE_UNICODE:04x}", children=[
        _string("CompanyName", "KiPI"),
        _string("FileDescription", description),
        _string("FileVersion", version),
        _string("InternalName", NAME),
        _string("OriginalFilename", EXE_NAME),
        _string("ProductName", NAME),
        _string("ProductVersion", version),
        _string("Comments", "The Python of KiPI's environment, named for KiPI"),
    ])
    strings = _node("StringFileInfo", children=[table])
    var = _node("VarFileInfo", children=[
        _node("Translation", struct.pack("<HH", LANG_EN_US, CODEPAGE_UNICODE))])
    return _node("VS_VERSION_INFO", fixed, children=[strings, var])


def icon_resources(ico: bytes):
    """An .ico file as the RT_GROUP_ICON directory and its RT_ICON images (ids 1...)."""
    reserved, kind, count = struct.unpack_from("<HHH", ico, 0)
    if reserved != 0 or kind != 1 or not count:
        raise ValueError("not an icon file")
    group = struct.pack("<HHH", 0, 1, count)
    images = []
    for i in range(count):
        w, h, colors, _, planes, bits, size, offset = struct.unpack_from("<BBBBHHII", ico,
                                                                         6 + 16 * i)
        images.append(ico[offset:offset + size])
        group += struct.pack("<BBBBHHIH", w, h, colors, 0, planes or 1, bits, size, i + 1)
    return group, images


def _existing(path: Path):
    """[(type, name, language)] of the icon, icon group and version resources in ``path``."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LoadLibraryExW.restype = wintypes.HMODULE
    k32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    k32.FreeLibrary.argtypes = [wintypes.HMODULE]
    name_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p,
                                   ctypes.c_void_p, ctypes.c_void_p)
    lang_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p,
                                   ctypes.c_void_p, wintypes.WORD, ctypes.c_void_p)
    k32.EnumResourceNamesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, name_proc,
                                       ctypes.c_void_p]
    k32.EnumResourceLanguagesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p,
                                           ctypes.c_void_p, lang_proc, ctypes.c_void_p]
    # LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE: resources only, no code.
    module = k32.LoadLibraryExW(str(path), None, 0x2 | 0x20)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    found = []
    try:
        for kind in (RT_ICON, RT_GROUP_ICON, RT_VERSION):
            names = []

            def on_name(_m, _t, name, _p, names=names):
                # An integer id is below 0x10000; anything else is a string's address.
                names.append(name if name < 0x10000 else ctypes.wstring_at(name))
                return True
            k32.EnumResourceNamesW(module, kind, name_proc(on_name), None)
            for name in names:
                def on_lang(_m, _t, _n, lang, _p, name=name, kind=kind):
                    found.append((kind, name, lang))
                    return True
                k32.EnumResourceLanguagesW(module, kind, name, lang_proc(on_lang), None)
    finally:
        k32.FreeLibrary(module)
    return found


def _strip_signature(path: Path) -> None:
    """Drop the Authenticode signature that came with Python: it no longer matches
    the changed file, and a broken signature looks worse than none."""
    data = bytearray(path.read_bytes())
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    magic = struct.unpack_from("<H", data, pe + 24)[0]
    dirs = pe + 24 + (96 if magic == 0x10B else 112)          # PE32 / PE32+
    entry = dirs + 4 * 8                                      # IMAGE_DIRECTORY_ENTRY_SECURITY
    offset, size = struct.unpack_from("<II", data, entry)
    if not size:
        return
    struct.pack_into("<II", data, entry, 0, 0)
    if offset + size >= len(data) - 8:                        # it is at the end of the file
        del data[offset:]
    path.write_bytes(bytes(data))


def write_named_exe(source: Path, target: Path, ico: Path = ICON) -> None:
    """Copy the program ``source`` to ``target`` with KiPI's icon and version resource."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.BeginUpdateResourceW.restype = wintypes.HANDLE
    k32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    k32.UpdateResourceW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.WORD, ctypes.c_void_p, wintypes.DWORD]
    k32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]

    group, images = icon_resources(ico.read_bytes())
    tmp = target.with_name(target.name + ".tmp")
    shutil.copyfile(source, tmp)
    try:
        _strip_signature(tmp)
        old = _existing(tmp)
        handle = k32.BeginUpdateResourceW(str(tmp), False)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        ok = False
        try:
            def update(kind, name, lang, data):
                ref = ctypes.c_wchar_p(name) if isinstance(name, str) else name
                buf = ctypes.create_string_buffer(data, len(data)) if data else None
                if not k32.UpdateResourceW(handle, kind, ref, lang, buf,
                                           len(data) if data else 0):
                    raise ctypes.WinError(ctypes.get_last_error())
            for kind, name, lang in old:              # Python's icon and version info
                update(kind, name, lang, None)
            for i, image in enumerate(images, 1):
                update(RT_ICON, i, LANG_EN_US, image)
            update(RT_GROUP_ICON, 1, LANG_EN_US, group)
            update(RT_VERSION, 1, LANG_EN_US, version_resource())
            ok = True
        finally:
            if not k32.EndUpdateResourceW(handle, not ok) and ok:
                raise ctypes.WinError(ctypes.get_last_error())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


# --- the copy in the venv -------------------------------------------------------------

def venv_home(scripts: Path) -> Optional[Path]:
    """The base Python's folder from the pyvenv.cfg of the venv whose Scripts folder is
    ``scripts``; None if it is not a venv's."""
    cfg = scripts.parent / "pyvenv.cfg"
    try:
        lines = cfg.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "home":
            return Path(value.strip())
    return None


def runtime_files(home: Path, version=None) -> Optional[List[Path]]:
    """What the copy needs from the base Python: its pythonw.exe first, then the DLLs it
    loads, which Windows looks for beside the program. None if any is missing."""
    v = version or sys.version_info
    needed = [home / "pythonw.exe", home / f"python{v[0]}{v[1]}.dll"]
    if not all(p.is_file() for p in needed):
        return None
    optional = ["python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"]
    return needed + [home / n for n in optional if (home / n).is_file()]


def _made_from(files: List[Path], ico: Path = ICON) -> dict:
    return {"files": [[str(p), p.stat().st_size, p.stat().st_mtime_ns] for p in files],
            "icon": [ico.stat().st_size, ico.stat().st_mtime_ns], "version": _version()}


def _same(a: Path, b: Path) -> bool:
    sa, sb = a.stat(), b.stat()
    return sa.st_size == sb.st_size and sa.st_mtime_ns == sb.st_mtime_ns


def _read_stamp(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def named_python(python: Path, platform: Optional[str] = None) -> Optional[Path]:
    """``KiPI.exe`` beside the venv's ``pythonw.exe`` ``python``, made or brought up to
    date if needed; None where there cannot be one, or it failed to start before."""
    if (platform or sys.platform) != "win32" or python.name.lower() != "pythonw.exe":
        return None
    try:
        scripts = python.parent
        home = venv_home(scripts)
        files = runtime_files(home) if home is not None else None
        if not files or not ICON.is_file() or scripts.resolve() == home.resolve():
            return None
        target = scripts / EXE_NAME
        stamp_path = scripts / STAMP_NAME
        made = _made_from(files)
        stamp = _read_stamp(stamp_path)
        copies = [target] + [scripts / f.name for f in files[1:]]
        if {k: stamp.get(k) for k in made} == made and all(c.is_file() for c in copies):
            return None if stamp.get("failed") else target
        for dll in files[1:]:
            copy = scripts / dll.name
            if copy.is_file() and _same(dll, copy):
                continue          # and may be in use: the venv's launcher loads it too
            tmp = scripts / (dll.name + ".tmp")
            shutil.copy2(dll, tmp)
            os.replace(tmp, copy)
        write_named_exe(files[0], target)
        stamp_path.write_text(json.dumps(made), encoding="utf-8")
        return target
    except Exception:  # noqa: BLE001 - cosmetic; KiPI runs on pythonw.exe instead
        _log("Could not make KiPI.exe", exc=True)
        return None


def mark_failed(python: Path) -> None:
    """Remember that the copy beside ``python`` did not start, until it is made again."""
    path = python.with_name(STAMP_NAME)
    stamp = _read_stamp(path)
    stamp["failed"] = True
    try:
        path.write_text(json.dumps(stamp), encoding="utf-8")
    except OSError:
        pass


def _log(text: str, exc: bool = False) -> None:
    import logging
    logging.getLogger("kipi").debug(text, exc_info=exc)


def started() -> bool:
    """In a KiPI that :func:`relaunch` started on ``KiPI.exe``: tell it so, and return
    True. False anywhere else."""
    ready = os.environ.pop(RELAUNCHED_ENV, None)
    if not ready:
        return False
    try:
        Path(ready).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    return True


def relaunch(args: List[str]) -> Optional[int]:
    """Run KiPI again on ``KiPI.exe`` with the arguments ``args`` (a script and its
    arguments, or ``-m kipi`` and its), on Windows in a venv, and return the code to
    exit with once it has started; None to go on in this process instead: on other
    platforms, when already on it, or when it cannot be made or does not start.

    The KiPI on ``KiPI.exe`` calls :func:`started` first thing. A copy that exits before
    that could not start Python (a DLL it could not load, say): KiPI then runs here, and
    the copy is not tried again until it is made anew."""
    if started() or sys.platform != "win32":
        return None
    python = Path(sys.executable)
    exe = named_python(python)
    if exe is None:
        return None
    ready = exe.with_name(f"KiPI-started-{os.getpid()}.tmp")
    env = dict(os.environ)
    env[RELAUNCHED_ENV] = str(ready)
    try:
        ready.unlink(missing_ok=True)
        child = subprocess.Popen([str(exe), *args], env=env,
                                 close_fds=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        _log("Could not start KiPI.exe", exc=True)
        mark_failed(python)
        return None
    try:
        deadline = time.monotonic() + STARTUP_WAIT_S
        while time.monotonic() < deadline:
            if ready.exists():
                return 0
            code = child.poll()
            if code is not None:
                if ready.exists():
                    return 0
                _log(f"KiPI.exe exited with {code:#x} before starting; running on "
                     f"{python.name}")
                mark_failed(python)
                return None
            time.sleep(0.02)
        return 0          # slow, but running: a second KiPI here would only wait for it
    finally:
        try:
            ready.unlink(missing_ok=True)
        except OSError:
            pass
