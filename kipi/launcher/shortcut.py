"""Start standalone KiPI like any other app, by an icon of its own instead of a Python file
(#127). A ``.py`` file cannot carry an icon of its own on any platform, so:

* Windows: the launcher keeps a ``KiPI.lnk`` in the KiPI folder, next to
  ``kipi_launcher.pyw``, so there is a KiPI icon to double-click from the first start on,
  and marks the folder so Explorer shows its ``desktop.ini`` icon. ``--create-shortcut``
  puts KiPI in the Start menu too. Both shortcuts carry KiPI's AppUserModelID, so a pin and
  the running KiPI share one taskbar button.
* Linux: ``--create-shortcut`` adds a ``kipi.desktop`` entry to the applications menu.
* macOS: ``--create-shortcut`` makes a small ``KiPI.app`` in ``~/Applications`` that runs
  the launcher.

Each runs this folder's launcher with the Python that is running it now. Standard library
only."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

from kipi.launcher.paths import KIPI_ROOT
from kipi.launcher.system import child_kwargs


class ShortcutError(RuntimeError):
    pass


def windows_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "KiPI.lnk"


def folder_lnk(root: Path = KIPI_ROOT) -> Path:
    """The shortcut kept in the KiPI folder (Windows)."""
    return root / "KiPI.lnk"


def linux_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    base = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else \
        Path.home() / ".local" / "share"
    return base / "applications" / "kipi.desktop"


def mac_path(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / "Applications" / "KiPI.app"


# The values go in through the environment, so no path ever has to be quoted for PowerShell.
_PS = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:KIPI_LNK); "
       "$s.TargetPath = $env:KIPI_TARGET; $s.Arguments = $env:KIPI_ARGS; "
       "$s.WorkingDirectory = $env:KIPI_DIR; $s.IconLocation = $env:KIPI_ICON; "
       "$s.Description = 'KiPI power integrity analysis (standalone)'; $s.Save()")


def desktop_entry(python: str, root: Path = KIPI_ROOT) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=KiPI",
        "Comment=Power integrity analysis of PCBs (standalone)",
        f'Exec="{python}" "{root / "kipi_launcher.py"}" %f',
        f"Icon={root / 'resources' / 'kipi_256.png'}",
        "Terminal=false",
        "StartupWMClass=KiPI",            # the launcher's and KiPI's windows
        "Categories=Science;Engineering;Electronics;",
        "",
    ])


def windows_target(root: Path) -> Tuple[Path, str]:
    """(program, arguments): the launcher with no console, by this Python."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    target = pythonw if pythonw.is_file() else Path(sys.executable)
    return target, f'"{root / "kipi_launcher.pyw"}"'


def _write_lnk(path: Path, root: Path) -> None:
    target, args = windows_target(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, KIPI_LNK=str(path), KIPI_TARGET=str(target), KIPI_ARGS=args,
               KIPI_DIR=str(root), KIPI_ICON=str(root / "resources" / "kipi.ico"))
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
                             env=env, capture_output=True, text=True, timeout=60,
                             **child_kwargs())
    except (OSError, subprocess.SubprocessError) as e:
        raise ShortcutError(f"could not run PowerShell: {e}") from e
    if out.returncode != 0 or not path.is_file():
        raise ShortcutError((out.stderr or out.stdout).strip()[-300:] or "PowerShell failed")
    try:
        from kipi.launcher.appid import set_shortcut_app_id
        set_shortcut_app_id(path)
    except Exception:  # noqa: BLE001 - the shortcut works without; a pin gets its own button
        pass


def icns(images: List[Tuple[str, bytes]]) -> bytes:
    """A macOS .icns of PNG images, given as ``(type, png bytes)``; ``ic08`` is 256 px."""
    body = b"".join(kind.encode("ascii") + (8 + len(data)).to_bytes(4, "big") + data
                    for kind, data in images)
    return b"icns" + (8 + len(body)).to_bytes(4, "big") + body


def info_plist() -> str:
    from kipi.launcher.appid import APP_ID
    keys = [("CFBundleName", "KiPI"), ("CFBundleDisplayName", "KiPI"),
            ("CFBundleIdentifier", APP_ID + ".launcher"), ("CFBundleExecutable", "KiPI"),
            ("CFBundleIconFile", "kipi"), ("CFBundlePackageType", "APPL"),
            ("CFBundleInfoDictionaryVersion", "6.0")]
    rows = "".join(f"  <key>{k}</key>\n  <string>{v}</string>\n" for k, v in keys)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            f'<plist version="1.0">\n<dict>\n{rows}'
            '  <key>NSHighResolutionCapable</key>\n  <true/>\n</dict>\n</plist>\n')


def mac_script(python: str, root: Path = KIPI_ROOT) -> str:
    """What the app runs. No arguments are passed on: old macOS adds a ``-psn_...`` one."""
    def quote(text: str) -> str:
        return "'" + text.replace("'", "'\\''") + "'"
    return f"#!/bin/sh\nexec {quote(python)} {quote(str(root / 'kipi_launcher.pyw'))}\n"


def _write_app(app: Path, root: Path) -> None:
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_text(info_plist(), encoding="utf-8")
    exe = contents / "MacOS" / "KiPI"
    exe.write_text(mac_script(sys.executable, root), encoding="utf-8")
    exe.chmod(0o755)
    png = root / "resources" / "kipi_256.png"
    if png.is_file():
        (contents / "Resources" / "kipi.icns").write_bytes(icns([("ic08", png.read_bytes())]))
    os.utime(app)                       # Finder looks at a changed bundle's icon again


def create(platform: Optional[str] = None, root: Path = KIPI_ROOT,
           home: Optional[Path] = None) -> Path:
    """Make the shortcut; returns where it is. Raises ShortcutError."""
    platform = platform or sys.platform
    if platform == "win32":
        path = windows_path()
        _write_lnk(path, root)
        return path
    try:
        if platform.startswith("linux"):
            path = linux_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(desktop_entry(sys.executable, root), encoding="utf-8")
            return path
        if platform == "darwin":
            path = mac_path(home)
            _write_app(path, root)
            return path
    except OSError as e:
        raise ShortcutError(str(e)) from e
    raise ShortcutError(f"making a shortcut is not supported on {platform}; start KiPI with "
                        f"python3 \"{root / 'kipi_launcher.py'}\"")


def keep_folder_icon(root: Path = KIPI_ROOT, stamp: Optional[Path] = None) -> None:
    """Windows: a KiPI.lnk in the KiPI folder, and the folder shown with the KiPI icon.
    Made again only when it is missing or the folder or the Python changed (``stamp``
    remembers which), so a PowerShell start is needed only then. Never raises: the .pyw
    still works without it (a read-only folder, no PowerShell)."""
    if sys.platform != "win32":
        return
    try:
        _mark_custom_folder(root)
    except Exception:  # noqa: BLE001 - cosmetic
        pass
    lnk = folder_lnk(root)
    want = "\n".join(str(p) for p in (root, *windows_target(root)))
    try:
        if lnk.is_file() and stamp is not None and stamp.is_file() \
                and stamp.read_text(encoding="utf-8") == want:
            return
        _write_lnk(lnk, root)
        if stamp is not None:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(want, encoding="utf-8")
    except (ShortcutError, OSError):
        pass


def _mark_custom_folder(root: Path) -> None:
    """Explorer reads a folder's desktop.ini (its icon) only when the folder is marked
    read-only or system and the file is hidden and system; git and unzipping set neither."""
    import ctypes

    ini = root / "desktop.ini"
    if not ini.is_file():
        return
    k32 = ctypes.windll.kernel32
    k32.GetFileAttributesW.restype = ctypes.c_uint32
    hidden_system, invalid = 0x2 | 0x4, 0xFFFFFFFF
    attrs = k32.GetFileAttributesW(str(ini))
    if attrs != invalid and attrs & hidden_system != hidden_system:
        k32.SetFileAttributesW(str(ini), attrs | hidden_system)
    folder = k32.GetFileAttributesW(str(root))
    if folder != invalid and not folder & (0x1 | 0x4):        # neither read-only nor system
        ctypes.windll.shlwapi.PathMakeSystemFolderW(str(root))
        # SHCNE_UPDATEDIR, SHCNF_PATHW: Explorer redraws the folder with its icon
        ctypes.windll.shell32.SHChangeNotify(0x00001000, 0x0005, str(root), None)
