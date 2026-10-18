"""Put standalone KiPI in the Start menu (Windows) or the applications menu (Linux), so
it starts like any other app. The shortcut runs this folder's launcher with the Python
that is running it now. On Windows it carries KiPI's AppUserModelID, so pinning it to the
taskbar and the running KiPI share one button."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

from kipi.launcher.paths import KIPI_ROOT
from kipi.launcher.system import child_kwargs


class ShortcutError(RuntimeError):
    pass


def windows_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "KiPI.lnk"


def linux_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    base = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else \
        Path.home() / ".local" / "share"
    return base / "applications" / "kipi.desktop"


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
        "Categories=Science;Engineering;Electronics;",
        "",
    ])


def create(platform: Optional[str] = None, root: Path = KIPI_ROOT) -> Path:
    """Make the shortcut; returns where it is. Raises ShortcutError."""
    platform = platform or sys.platform
    if platform == "win32":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        target = pythonw if pythonw.is_file() else Path(sys.executable)
        path = windows_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, KIPI_LNK=str(path), KIPI_TARGET=str(target),
                   KIPI_ARGS=f'"{root / "kipi_launcher.pyw"}"', KIPI_DIR=str(root),
                   KIPI_ICON=str(root / "resources" / "kipi.ico"))
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                  _PS], env=env, capture_output=True, text=True, timeout=60,
                                 **child_kwargs())
        except (OSError, subprocess.SubprocessError) as e:
            raise ShortcutError(f"could not run PowerShell: {e}") from e
        if out.returncode != 0 or not path.is_file():
            raise ShortcutError((out.stderr or out.stdout).strip()[-300:] or "PowerShell failed")
        try:
            from kipi.gui.identity import set_shortcut_app_id    # standard library only
            set_shortcut_app_id(path)
        except Exception:  # noqa: BLE001 - the shortcut works without; a pin gets its own button
            pass
        return path
    if platform.startswith("linux"):
        path = linux_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(desktop_entry(sys.executable, root), encoding="utf-8")
        except OSError as e:
            raise ShortcutError(str(e)) from e
        return path
    raise ShortcutError("making a shortcut is not supported on macOS yet; start KiPI with "
                        f"python3 \"{root / 'kipi_launcher.py'}\"")
