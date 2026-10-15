"""KiPI names itself and uses its own icon in the taskbar, Dock or panel."""

import os
import sys

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from kipi.gui import identity  # noqa: E402


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
    assert identity.read_windows_property(h, identity._PKEY_ID) == identity.APP_ID
    assert identity.read_windows_property(h, identity._PKEY_RELAUNCH_DISPLAY_NAME) == "KiPI"
    w.close()
