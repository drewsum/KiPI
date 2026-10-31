"""The launcher's setup and start: installing into KiPI's environment, the console screen,
and what the launcher does on each start."""

import os
import sys
import threading
import time
from pathlib import Path, PurePosixPath

import pytest

from kipi.launcher import deps, install, main as launcher, paths, setup_env, state
from kipi.launcher.install import Canceled, InstallError, Installer
from kipi.launcher.setup_env import Progress, Summary
from kipi.launcher.system import Python
from kipi.launcher.ui_console import ConsoleUI

PY = Python(sys.executable, (3, 12, 1), True)


def _summary(env, pardiso_offered=True, default=False):
    plan = deps.Plan([deps.Package("numpy", "2.5.4", 13_000_000)],
                     [deps.Package("pypardiso", "0.4.7", 10_000),
                      deps.Package("mkl", "2026.2.0", 185_000_000)], "live")
    return Summary("KiPI's environment is not set up yet", PY, env, plan, pardiso_offered,
                   "" if pardiso_offered else "Intel MKL is not available for Darwin", default)


# --- Running installers -------------------------------------------------------------------

def test_an_installer_passes_on_every_line(tmp_path):
    lines = []
    inst = Installer(PY, tmp_path / "venv", lines.append)
    inst._run([sys.executable, "-c", "print('Collecting numpy'); print(); print('done')"])
    assert lines[0].startswith("$ ") and lines[1:] == ["Collecting numpy", "done"]


def test_a_failing_installer_says_what_it_printed_last(tmp_path):
    inst = Installer(PY, tmp_path / "venv", lambda line: None)
    with pytest.raises(InstallError, match="No matching distribution"):
        inst._run([sys.executable, "-c", "import sys; print('ERROR: No matching distribution "
                   "found for triangle'); sys.exit(1)"])


def test_cancel_stops_the_running_installer(tmp_path):
    inst = Installer(PY, tmp_path / "venv", lambda line: None)
    threading.Timer(0.5, inst.cancel).start()
    t0 = time.monotonic()
    with pytest.raises(Canceled):
        inst._run([sys.executable, "-c", "import time; print('working', flush=True); "
                   "time.sleep(30)"])
    assert time.monotonic() - t0 < 15
    with pytest.raises(Canceled):                       # and nothing runs after it
        inst._run([sys.executable, "-c", "pass"])


def test_only_kipis_environment_is_ever_deleted(tmp_path):
    data = tmp_path / "KiPI"
    env = data / "venv"
    env.mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = x\n")
    (env / "lib").mkdir()
    install.remove_env(env, data)
    assert not env.exists()
    install.remove_env(env, data)                       # already gone: fine

    other = data / "projects"
    other.mkdir()
    with pytest.raises(InstallError, match="refusing"):
        install.remove_env(other, data)
    env.mkdir()
    (env / "my_files.txt").write_text("keep me")
    with pytest.raises(InstallError, match="not a virtual environment"):
        install.remove_env(env, data)
    assert (env / "my_files.txt").exists()


def test_progress_follows_pip():
    p = Progress(4)
    assert p.feed("Collecting numpy>=1.24") == pytest.approx(0.85 / 4)
    assert p.feed("  Downloading numpy-2.5.4.whl (13 MB)") is None
    assert p.feed("Installing collected packages: numpy") == 0.9
    assert p.feed("Successfully installed numpy-2.5.4") == 1.0


# --- One setup ----------------------------------------------------------------------------

class FakeInstaller:
    made = []

    def __init__(self, python, env, on_line, uv=None, environ=None):
        self.env, self.uv, self.calls, self.environ = env, uv, [], environ
        self.fail = FakeInstaller.fail_with_uv and uv is not None
        FakeInstaller.made.append(self)

    tool = property(lambda self: "uv" if self.uv else "pip")
    fail_with_uv = False

    def create_env(self):
        self.calls.append("create")
        if self.fail:
            raise InstallError("uv: no interpreter")
        py = paths.env_python(self.env)
        py.parent.mkdir(parents=True, exist_ok=True)
        py.write_text("")
        (self.env / "pyvenv.cfg").write_text("")

    def install(self, requirements=None, packages=()):
        self.calls.append(("install", tuple(packages)))

    def uninstall(self, packages):
        self.calls.append(("uninstall", tuple(packages)))


@pytest.fixture
def fake_installer(monkeypatch):
    FakeInstaller.made = []
    FakeInstaller.fail_with_uv = False
    monkeypatch.setattr(setup_env, "Installer", FakeInstaller)
    return FakeInstaller


def _perform(tmp_path, pardiso, uv=None, fresh=False):
    data = tmp_path / "KiPI"
    data.mkdir(exist_ok=True)
    req = paths.requirements_file()
    fractions = []
    st = setup_env.perform(_summary(data / "venv"), pardiso, data, req, lambda line: None,
                           fractions.append, lambda inst: None, fresh, uv)
    return st, fractions


def test_a_first_setup_makes_the_environment_and_records_it(tmp_path, fake_installer):
    st, fractions = _perform(tmp_path, pardiso=True)
    inst = fake_installer.made[-1]
    assert inst.calls == ["create", ("install", ("pypardiso",))]
    assert st.pardiso and st.tool == "pip" and st.python == "3.12.1"
    env = tmp_path / "KiPI" / "venv"
    assert state.load(env) == st
    assert state.needs_setup(env, deps.requirements_hash(paths.requirements_file())) is None
    assert fractions[-1] == 1.0


def test_dropping_pardiso_uninstalls_it(tmp_path, fake_installer):
    _perform(tmp_path, pardiso=True)
    st, _ = _perform(tmp_path, pardiso=False)
    calls = fake_installer.made[-1].calls
    assert "create" not in calls                         # the environment is kept
    assert calls[-1] == ("uninstall", ("pypardiso", "mkl"))
    assert not st.pardiso


def test_when_uv_fails_pip_starts_again(tmp_path, fake_installer):
    fake_installer.fail_with_uv = True
    st, _ = _perform(tmp_path, pardiso=False, uv="uv.exe")
    first, second = fake_installer.made
    assert first.uv == "uv.exe" and first.calls == ["create"]
    assert second.uv is None and second.calls[0] == "create"
    assert st.tool == "pip"


def test_uv_is_found_on_the_path_or_through_uvx_or_pipx(tmp_path):
    # #113: uv when there is one, else uvx or pipx fetching it, else venv and pip.
    def which(*have):
        return lambda name: str(tmp_path / f"{name}.exe") if name in have else None
    assert install.find_uv(which("uv", "uvx", "pipx")) == [str(tmp_path / "uv.exe")]
    assert install.find_uv(which("uvx", "pipx")) == [str(tmp_path / "uvx.exe"), "uv"]
    (tmp_path / "uv.exe").write_text("")          # uv beside uvx, just not on the PATH
    assert install.find_uv(which("uvx")) == [str(tmp_path / "uv.exe")]
    assert install.find_uv(which("pipx")) == [str(tmp_path / "pipx.exe"), "run", "uv"]
    assert install.find_uv(which()) is None
    assert install.uv_label(["/x/uv"]) == "uv"
    assert install.uv_label(["C:/x/pipx.exe", "run", "uv"]) == "uv (pipx)"


def test_installer_runs_uv_through_pipx(tmp_path):
    ran = []
    inst = install.Installer(_summary(tmp_path / "venv").python, tmp_path / "venv",
                             lambda line: None, ["pipx", "run", "uv"])
    inst._run = ran.append
    inst.install(packages=["numpy"])
    assert ran[0][:5] == ["pipx", "run", "uv", "pip", "install"] and ran[0][-1] == "numpy"
    assert inst.tool == "uv (pipx)"


def test_a_real_environment_is_made(tmp_path):
    """venv and pip for real, installing nothing (no network needed)."""
    data = tmp_path / "KiPI"
    data.mkdir()
    req = tmp_path / "requirements.txt"
    req.write_text("# nothing\n")
    s = _summary(data / "venv")
    lines = []
    st = setup_env.perform(s, False, data, req, lines.append, lambda f: None,
                           lambda inst: None, uv=None)
    assert paths.env_python(data / "venv").is_file() and st.tool == "pip"
    assert state.needs_setup(data / "venv", deps.requirements_hash(req)) is None


# --- The console screen -------------------------------------------------------------------

def _console(answers):
    out, it = [], iter(answers)

    def ask(prompt):
        out.append(prompt)
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None
    return ConsoleUI(out.append, ask), out


def test_the_console_lists_everything_and_asks(tmp_path):
    ui, out = _console(["y", ""])
    assert ui.confirm(_summary(tmp_path)) is True
    text = "\n".join(out)
    assert "numpy" in text and "13 MB" in text and "PARDISO" in text
    assert "185 MB" not in text.split("PARDISO")[0]       # MKL is not in the main list
    assert "With PARDISO: 3 packages" in text


def test_the_console_defaults(tmp_path):
    ui, _ = _console(["", ""])
    assert ui.confirm(_summary(tmp_path)) is False             # PARDISO is opt-in
    ui, _ = _console(["", ""])
    assert ui.confirm(_summary(tmp_path, default=True)) is True  # as chosen last time
    ui, _ = _console(["n", "n"])
    assert ui.confirm(_summary(tmp_path)) is None
    ui, _ = _console([])
    assert ui.confirm(_summary(tmp_path)) is None              # nobody to ask


def test_the_console_says_why_pardiso_is_not_offered(tmp_path):
    ui, out = _console(["y"])
    assert ui.confirm(_summary(tmp_path, pardiso_offered=False)) is False
    assert any("not available here: Intel MKL is not available for Darwin" in o for o in out)


# --- Each start ---------------------------------------------------------------------------

@pytest.fixture
def started(tmp_path, monkeypatch):
    """The launcher with a scratch home, a stand-in setup and no KiPI started."""
    monkeypatch.setenv("KIPI_HOME", str(tmp_path / "KiPI"))
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    got = {"launched": [], "performed": [], "answers": ["n", "y"], "out": [], "icon": []}
    from kipi.launcher import shortcut              # never a real KiPI.lnk in the repo
    monkeypatch.setattr(shortcut, "keep_folder_icon",
                        lambda root=None, stamp=None: got["icon"].append(stamp))
    monkeypatch.setattr(launcher.system, "find_python", lambda commands=None: (PY, []))
    monkeypatch.setattr(launcher.system, "pardiso_supported", lambda: (True, ""))
    monkeypatch.setattr(launcher.deps, "make_plan",
                        lambda python, req, offer: _summary(tmp_path).plan)

    def perform(summary, choice, data, req, on_line, on_fraction, register, fresh):
        got["performed"].append((choice, fresh))
        env = summary.env
        py = paths.env_python(env)
        py.parent.mkdir(parents=True, exist_ok=True)
        py.write_text("")
        (env / "pyvenv.cfg").write_text("")
        st = state.State(requirements_hash=deps.requirements_hash(req), pardiso=choice)
        state.save(env, st)
        return st

    monkeypatch.setattr(launcher, "perform", perform)
    monkeypatch.setattr(launcher, "launch",
                        lambda env, board, windowed: got["launched"].append(board) or 0)

    def make_ui(console, windowed):
        ui, out = _console(got["answers"])
        got["out"] = out
        return ui
    monkeypatch.setattr(launcher, "make_ui", make_ui)
    return got


def test_the_first_start_sets_up_then_starts_kipi(started):
    assert launcher.main(["board.kipi.json.gz"]) == 0
    assert started["performed"] == [(False, False)]
    assert started["launched"] == ["board.kipi.json.gz"]
    started["answers"] = []
    assert launcher.main([]) == 0                        # ready: straight to KiPI
    assert len(started["performed"]) == 1 and started["launched"][-1] is None


def test_saying_no_changes_nothing(started):
    started["answers"] = ["n", "n"]
    assert launcher.main([]) == 1
    assert started["performed"] == [] and started["launched"] == []


def test_pardiso_can_be_added_and_removed_without_questions(started):
    launcher.main([])
    started["answers"] = []
    assert launcher.main(["--add-pardiso", "--no-launch"]) == 0
    assert started["performed"][-1] == (True, False)
    assert launcher.main(["--remove-pardiso", "--no-launch"]) == 0
    assert started["performed"][-1] == (False, False)


def test_pardiso_is_refused_where_mkl_is_not(started, monkeypatch):
    monkeypatch.setattr(launcher.system, "pardiso_supported", lambda: (False, "no MKL"))
    assert launcher.main(["--add-pardiso"]) == 2
    assert "not available here: no MKL" in "\n".join(started["out"])


def test_repair_starts_from_scratch_and_reset_deletes(started, tmp_path):
    launcher.main([])
    started["answers"] = ["", ""]
    assert launcher.main(["--repair", "--no-launch"]) == 0
    assert started["performed"][-1] == (False, True)
    assert launcher.main(["--reset"]) == 0
    assert not (tmp_path / "KiPI" / "venv").exists()


def test_no_python_to_use(started, monkeypatch):
    monkeypatch.setattr(launcher.system, "find_python",
                        lambda commands=None: (None, ["/usr/bin/python3 is Python 3.9.2"]))
    assert launcher.main([]) == 2
    text = "\n".join(started["out"])
    assert "Python 3.10 to 3.13" in text and "3.9.2" in text


def test_a_failed_setup_points_at_the_log(started, monkeypatch):
    def fail(*a):
        raise InstallError("pip failed (exit 1): no space left on device")
    monkeypatch.setattr(launcher, "perform", fail)
    assert launcher.main([]) == 1
    text = "\n".join(started["out"])
    assert "no space left" in text and "launcher-" in text
    assert started["launched"] == []


def test_kicad_starting_the_launcher_runs_the_plugin(started, monkeypatch):
    class Entry:
        @staticmethod
        def main():
            return 42
    monkeypatch.setitem(sys.modules, "kipi_entry", Entry)
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc://x")
    assert launcher.main([]) == 42 and started["launched"] == []


def test_kipi_starts_standalone_from_this_folder(tmp_path, monkeypatch):
    env = tmp_path / "venv"
    py = paths.env_python(env)
    py.parent.mkdir(parents=True)
    py.write_text("")
    calls = []
    monkeypatch.setattr(launcher.subprocess, "call",
                        lambda cmd, env=None: calls.append((cmd, env)) or 0)
    monkeypatch.setenv("PYTHONPATH", "elsewhere")
    assert launcher.launch(env, "b.kipi.json.gz", windowed=False) == 0
    cmd, environ = calls[0]
    assert cmd[:3] == [str(py), "-m", "kipi"] and cmd[3] == str(Path("b.kipi.json.gz").resolve())
    assert environ["PYTHONPATH"].split(os.pathsep) == [str(paths.KIPI_ROOT), "elsewhere"]
    assert environ["KIPI_HOST"] == "standalone"
    assert environ[launcher.LAUNCHER_ENV].endswith("kipi_launcher.py")


# --- python -m kipi without the packages -----------------------------------------------------

def test_python_m_kipi_points_at_the_launcher(monkeypatch, capsys):
    import importlib.util
    from kipi import __main__ as cli
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name in ("PySide6", "pyamg") else real(name, *a))
    assert cli.main([]) == 2
    err = capsys.readouterr().err
    assert "PySide6, pyamg are missing" in err and "kipi_launcher.py" in err


# --- Waiting for KiPI, shortcuts, and KiPI asking for its setup ------------------------------

def test_waiting_for_a_process_to_end():
    from kipi.launcher import system
    import subprocess
    quick = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
    assert system.wait_for_exit(quick.pid, timeout=20)
    slow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert not system.wait_for_exit(slow.pid, timeout=0.5)
    finally:
        slow.kill()
        slow.wait()


def test_kipi_asking_for_its_setup_waits_for_it_to_close(started, monkeypatch):
    from kipi.launcher import system
    launcher.main([])
    monkeypatch.setattr(system, "wait_for_exit", lambda pid, timeout=60.0: False)
    assert launcher.main(["--setup", "--after-pid", "123"]) == 1
    assert "still open" in "\n".join(started["out"])
    waited = []
    monkeypatch.setattr(system, "wait_for_exit",
                        lambda pid, timeout=60.0: waited.append(pid) or True)
    started["answers"] = ["n", "n"]                     # change nothing after all
    assert launcher.main(["--setup", "--after-pid", "123", "--after-pid", "45"]) == 0
    assert waited == [123, 45]
    assert started["launched"][-1] is None              # KiPI opens again anyway


def test_the_shortcut_option(started, monkeypatch, tmp_path):
    from kipi.launcher import shortcut
    monkeypatch.setattr(shortcut, "create", lambda: tmp_path / "KiPI.lnk")
    assert launcher.main(["--create-shortcut"]) == 0
    assert "KiPI.lnk" in "\n".join(started["out"]) and started["launched"] == []

    def fail():
        raise shortcut.ShortcutError("not supported on macOS yet")
    monkeypatch.setattr(shortcut, "create", fail)
    assert launcher.main(["--create-shortcut"]) == 2


def test_a_linux_menu_entry(tmp_path, monkeypatch):
    from kipi.launcher import shortcut
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    path = shortcut.create("linux", root=Path("/opt/KiPI"))
    assert path == tmp_path / "applications" / "kipi.desktop"
    text = path.read_text()
    assert f'Exec="{sys.executable}" "{Path("/opt/KiPI/kipi_launcher.py")}" %f' in text
    assert "Name=KiPI" in text and "Terminal=false" in text
    assert "StartupWMClass=KiPI" in text                # groups with the launcher's window


def test_a_macos_app_that_runs_the_launcher(tmp_path):
    """#127: an app with the KiPI icon in ~/Applications, instead of a Python file."""
    from kipi.launcher import shortcut
    path = shortcut.create("darwin", home=tmp_path)
    assert path == tmp_path / "Applications" / "KiPI.app"
    contents = path / "Contents"
    plist = (contents / "Info.plist").read_text()
    assert "<string>KiPI</string>" in plist and "<key>CFBundleIconFile</key>" in plist
    script = (contents / "MacOS" / "KiPI").read_text()
    assert script.startswith("#!/bin/sh\nexec ") and "kipi_launcher.pyw" in script
    assert sys.executable in script
    if sys.platform != "win32":
        assert os.access(contents / "MacOS" / "KiPI", os.X_OK)
    icns = (contents / "Resources" / "kipi.icns").read_bytes()
    png = (paths.KIPI_ROOT / "resources" / "kipi_256.png").read_bytes()
    assert icns[:4] == b"icns" and int.from_bytes(icns[4:8], "big") == len(icns)
    assert icns[8:12] == b"ic08" and icns[16:] == png


def test_the_mac_script_quotes_paths_with_spaces_and_quotes():
    from kipi.launcher import shortcut
    text = shortcut.mac_script("/opt/my python/bin/python3", PurePosixPath("/Users/a/it's KiPI"))
    assert "exec '/opt/my python/bin/python3' '/Users/a/it'\\''s KiPI/kipi_launcher.pyw'" \
        in text


def test_other_platforms_say_how_to_start_kipi_instead():
    from kipi.launcher import shortcut
    with pytest.raises(shortcut.ShortcutError, match="kipi_launcher.py"):
        shortcut.create("freebsd14")


def test_every_start_keeps_the_folder_shortcut(started, tmp_path):
    assert launcher.main(["--console"]) == 0
    assert started["icon"] == [tmp_path / "KiPI" / "folder-shortcut.txt"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcut")
def test_a_kipi_icon_in_the_kipi_folder(tmp_path, monkeypatch):
    """#127: PowerShell for real, in a scratch copy of the folder; made once, not every start."""
    from kipi.launcher import shortcut
    root, stamp = tmp_path / "KiPI folder", tmp_path / "data" / "stamp.txt"
    root.mkdir()
    (root / "desktop.ini").write_text("[.ShellClassInfo]\nIconResource=resources\\kipi.ico,0\n")
    shortcut.keep_folder_icon(root, stamp)
    lnk = root / "KiPI.lnk"
    assert lnk.is_file() and stamp.is_file()
    from kipi.launcher import appid
    assert appid.read_file_property(lnk, appid.PKEY_ID) == appid.APP_ID
    import ctypes
    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(root / "desktop.ini"))
    assert attrs & 0x6 == 0x6                               # hidden and system
    assert ctypes.windll.kernel32.GetFileAttributesW(str(root)) & 0x5   # read-only or system
    monkeypatch.setattr(shortcut, "_write_lnk", lambda *a: pytest.fail("made again"))
    shortcut.keep_folder_icon(root, stamp)
    lnk.unlink()                                            # gone: made again, quietly
    monkeypatch.setattr(shortcut, "_write_lnk",
                        lambda *a: (_ for _ in ()).throw(shortcut.ShortcutError("no")))
    shortcut.keep_folder_icon(root, stamp)                  # never raises


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Start menu")
def test_a_windows_start_menu_shortcut(tmp_path, monkeypatch):
    """PowerShell for real, writing into a scratch folder instead of the Start menu."""
    from kipi.launcher import shortcut
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = shortcut.create("win32")
    assert path == tmp_path / "Microsoft/Windows/Start Menu/Programs/KiPI.lnk"
    assert path.is_file() and path.stat().st_size > 0
    from kipi.launcher import appid          # pinned, it shares the window's taskbar button
    assert appid.read_file_property(path, appid.PKEY_ID) == appid.APP_ID


def test_kipi_learns_which_python_ran_the_launcher(tmp_path, monkeypatch):
    env = tmp_path / "venv"
    py = paths.env_python(env)
    py.parent.mkdir(parents=True)
    py.write_text("")
    calls = []
    monkeypatch.setattr(launcher.subprocess, "call",
                        lambda cmd, env=None: calls.append(env) or 0)
    launcher.launch(env, None, windowed=False)
    assert calls[0][launcher.LAUNCHER_PYTHON_ENV] == sys.executable


def test_a_system_python_comes_before_the_environments_own(tmp_path, monkeypatch):
    """uv makes environments with no pip, which cannot list what to install."""
    env = tmp_path / "venv"
    py = paths.env_python(env)
    py.parent.mkdir(parents=True)
    py.write_text("")
    asked = []
    monkeypatch.setattr(launcher.system, "candidates", lambda: [["system-python"]])
    monkeypatch.setattr(launcher.system, "find_python",
                        lambda commands=None: asked.append(commands) or (PY, []))
    launcher.choose_python(env, fresh=False)
    assert asked[-1] == [["system-python"], [str(py)]]
    launcher.choose_python(env, fresh=True)             # a new environment: never its own
    assert asked[-1] == [["system-python"]]
