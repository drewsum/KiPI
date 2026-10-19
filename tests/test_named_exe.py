"""KiPI shows as "KiPI" in Windows Task Manager: it runs on KiPI.exe, a copy of the venv's
base Python with KiPI's icon and version information (#124)."""

import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from kipi.launcher import named_exe


def test_icon_file_becomes_a_group_and_its_images():
    group, images = named_exe.icon_resources(named_exe.ICON.read_bytes())
    _, kind, count = struct.unpack_from("<HHH", group)
    assert kind == 1 and count == len(images) > 1
    ids = [struct.unpack_from("<H", group, 6 + 14 * i + 12)[0] for i in range(count)]
    assert ids == list(range(1, count + 1))
    with pytest.raises(ValueError):
        named_exe.icon_resources(b"\0" * 16)


def test_version_resource_names_kipi():
    res = named_exe.version_resource(version="0.9.0")
    assert struct.unpack_from("<H", res)[0] == len(res)
    assert "VS_VERSION_INFO".encode("utf-16-le") in res
    for text in ("FileDescription", "KiPI", "0.9.0", "040904b0"):
        assert text.encode("utf-16-le") in res


def test_only_a_windows_venv_pythonw_gets_a_copy(tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "pythonw.exe").write_bytes(b"")
    assert named_exe.named_python(scripts / "pythonw.exe", platform="linux") is None
    assert named_exe.named_python(scripts / "python.exe", platform="win32") is None
    assert named_exe.named_python(scripts / "pythonw.exe", platform="win32") is None  # no cfg
    (tmp_path / "pyvenv.cfg").write_text(f"home = {tmp_path / 'nowhere'}\n")
    assert named_exe.venv_home(scripts) == tmp_path / "nowhere"
    assert named_exe.named_python(scripts / "pythonw.exe", platform="win32") is None


def test_relaunch_stays_put_when_it_is_the_copy(monkeypatch, tmp_path):
    ready = tmp_path / "ready"
    monkeypatch.setenv(named_exe.RELAUNCHED_ENV, str(ready))
    assert named_exe.relaunch(["kipi_entry.py"]) is None
    assert ready.read_text() == str(os.getpid())
    assert named_exe.RELAUNCHED_ENV not in os.environ


def _base_home() -> Path:
    return Path(getattr(sys, "_base_executable", sys.executable)).parent


@pytest.mark.skipif(sys.platform != "win32", reason="Windows executables")
def test_copy_runs_the_venv_and_says_kipi(tmp_path):
    home = _base_home()
    if named_exe.runtime_files(home) is None:
        pytest.skip("no pythonw.exe and Python DLL beside the base Python")
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (tmp_path / "venv" / "pyvenv.cfg").write_text(f"home = {home}\n")
    launcher = scripts / "pythonw.exe"
    launcher.write_bytes(b"")
    exe = named_exe.named_python(launcher)
    assert exe == scripts / "KiPI.exe"
    assert _file_description(exe) == "KiPI"
    out = tmp_path / "out.txt"
    subprocess.run([str(exe), "-c", f"import sys; open({str(out)!r}, 'w').write(sys.prefix)"],
                   check=True, timeout=60)
    assert Path(out.read_text()) == tmp_path / "venv"
    stamp = (scripts / named_exe.STAMP_NAME).read_text()
    assert named_exe.named_python(launcher) == exe                      # made once
    assert (scripts / named_exe.STAMP_NAME).read_text() == stamp
    named_exe.mark_failed(launcher)
    assert named_exe.named_python(launcher) is None                     # not tried again
    (scripts / "KiPI.exe").unlink()
    assert named_exe.named_python(launcher) == exe                      # until made anew


def _file_description(path: Path) -> str:
    import ctypes
    ver = ctypes.windll.version
    size = ver.GetFileVersionInfoSizeW(str(path), None)
    buf = ctypes.create_string_buffer(size)
    assert ver.GetFileVersionInfoW(str(path), 0, size, buf)
    value, n = ctypes.c_wchar_p(), ctypes.c_uint()
    assert ver.VerQueryValueW(buf, r"\StringFileInfo\040904b0\FileDescription",
                              ctypes.byref(value), ctypes.byref(n))
    return value.value
