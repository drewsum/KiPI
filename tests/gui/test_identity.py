"""KiPI names itself and uses its own icon in the taskbar, Dock or panel."""

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from kipi.gui import identity  # noqa: E402
from kipi.launcher import appid  # noqa: E402


def test_app_is_named_kipi_with_its_icon():
    app = identity.make_app([])
    assert app.applicationName() == "KiPI" and app.desktopFileName() == "kipi"
    assert 256 in [s.width() for s in app.windowIcon().availableSizes()]


def test_icon_files_ship_with_the_plugin():
    names = [p.name for p in identity.icon_files()]
    assert "kipi.ico" in names and "kipi_256.png" in names


def test_linux_desktop_entry_is_hidden_and_matches_the_window():
    text = identity.desktop_entry()
    assert "Name=KiPI" in text and "NoDisplay=true" in text
    assert "StartupWMClass=KiPI" in text and "kipi_256.png" in text


def test_a_pinned_button_runs_the_launcher(monkeypatch, tmp_path):
    monkeypatch.delenv("KIPI_LAUNCHER_PYTHON", raising=False)
    cmd = identity.relaunch_command()
    assert cmd.endswith(f'"{identity.ROOT / "kipi_launcher.pyw"}"')
    assert str(Path(sys.executable).parent) in cmd
    python = tmp_path / "python.exe"
    (tmp_path / "pythonw.exe").write_text("")
    monkeypatch.setenv("KIPI_LAUNCHER_PYTHON", str(python))
    assert identity.relaunch_command().startswith(f'"{tmp_path / "pythonw.exe"}" ')
    monkeypatch.setattr(identity, "ROOT", tmp_path)            # no launcher: no pinning
    assert identity.relaunch_command() is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskbar properties")
def test_windows_taskbar_button_is_kipi():
    from qtpy.QtWidgets import QWidget
    app = identity.make_app([])
    if app.platformName() != "windows":
        pytest.skip("needs a real window")
    w = QWidget()
    w.show()
    identity.tag_window(w)
    h = int(w.winId())
    assert appid.read_windows_property(h, appid.PKEY_ID) == identity.APP_ID
    assert appid.read_windows_property(h, appid.PKEY_RELAUNCH_DISPLAY_NAME) == "KiPI"
    assert appid.read_windows_property(h, appid.PKEY_PREVENT_PINNING) is False
    assert "kipi_launcher.pyw" in appid.read_windows_property(
        h, appid.PKEY_RELAUNCH_COMMAND)
    w.close()


def test_the_launchers_menu_entry_is_not_overwritten(tmp_path, monkeypatch):
    """Both are kipi.desktop; --create-shortcut's visible one serves the window as well."""
    from kipi.launcher import shortcut
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    path = shortcut.create("linux")
    menu = path.read_text()
    identity._write_desktop_file()
    assert path.read_text() == menu
    path.unlink()
    identity._write_desktop_file()
    assert "NoDisplay=true" in path.read_text()
