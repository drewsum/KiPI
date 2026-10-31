"""KiCad installs only kicad-python; KiPI's first click sets up the rest, and standalone
KiPI uses KiCad's environment when it lives in KiCad's plugin folders (#7)."""

import json
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from kipi.launcher import deps, kicad_env, main as launcher, paths, plugin_setup, setup_env, state
from kipi.launcher.install import InstallError
from kipi.launcher.system import Python
from kipi.launcher.ui_native import NativeUI, SilentUI, question_text

PY = Python(sys.executable, (3, 11, 5), True)
REQ = paths.requirements_file()


def _env(tmp_path, home_exists=True):
    """A stand-in virtual environment, made from a base Python that is (or is not) there."""
    env = tmp_path / "python-environments" / kicad_env.IDENTIFIER
    py = paths.env_python(env)
    py.parent.mkdir(parents=True)
    py.write_text("")
    home = tmp_path / "KiCad" / "bin"
    if home_exists:
        home.mkdir(parents=True)
    (env / "pyvenv.cfg").write_text(f"home = {home}\ninclude-system-site-packages = true\n")
    return env


# --- Where KiCad keeps it -------------------------------------------------------------------

@pytest.mark.parametrize("root, version", [
    (PureWindowsPath(r"C:\Users\a\OneDrive\Documents\KiCad\10.0\3rdparty\plugins"
                     r"\com_github_drewsum_kipi"), "10.0"),
    (PurePosixPath("/home/a/.local/share/kicad/9.0/3rdparty/plugins/com_github_drewsum_kipi"),
     "9.0"),
    (PurePosixPath("/Users/a/Documents/KiCad/10.0/plugins/KiPI"), "10.0"),
    (PurePosixPath("/home/a/src/KiPI"), None),
    (PurePosixPath("/home/a/plugins/KiPI"), None),          # no version folder above
])
def test_kipi_knows_which_kicad_installed_it(root, version):
    assert kicad_env.kicad_version(root) == version


def test_kicads_cache_folder_on_each_platform(tmp_path):
    home = Path("/home/a")
    assert kicad_env.plugin_env("10.0", environ={"LOCALAPPDATA": r"C:\Users\a\AppData\Local"},
                                platform="win32", home=home) == \
        Path(r"C:\Users\a\AppData\Local") / "KiCad" / "10.0" / "python-environments" / \
        "com.github.drewsum.kipi"
    assert kicad_env.cache_dir(environ={}, platform="darwin", home=home) == \
        home / "Library" / "Caches" / "kicad"
    assert kicad_env.cache_dir(environ={}, platform="linux", home=home) == home / ".cache" / "kicad"
    assert kicad_env.cache_dir(environ={"XDG_CACHE_HOME": "/x"}, platform="linux", home=home) \
        == Path("/x") / "kicad"
    assert kicad_env.cache_dir(environ={"KICAD_CACHE_HOME": "/k", "XDG_CACHE_HOME": "/x"},
                               platform="linux", home=home) == Path("/k") / "kicad"


def test_the_identifier_is_the_plugins():
    plugin = json.loads((paths.KIPI_ROOT / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["identifier"] == kicad_env.IDENTIFIER
    assert kicad_env.PCM_FOLDER == "com_github_drewsum_kipi"


def test_an_environment_whose_kicad_is_gone_is_not_used(tmp_path, monkeypatch):
    env = _env(tmp_path, home_exists=False)
    assert "gone" in kicad_env.base_python_missing(env)
    root = tmp_path / "KiCad" / "10.0" / "3rdparty" / "plugins" / kicad_env.PCM_FOLDER
    monkeypatch.setattr(kicad_env, "plugin_env", lambda version, **kw: env)
    found, why = kicad_env.find(root)
    assert found is None and "gone" in why
    (tmp_path / "KiCad" / "bin").mkdir(parents=True)
    assert kicad_env.find(root) == (env, "")


# --- What is missing ------------------------------------------------------------------------

def test_requirements_are_met_by_name_and_version(tmp_path):
    req = tmp_path / "req.txt"
    req.write_text("# c\nPySide6-Essentials>=6.6\nnumpy>=1.24\nscipy>=1.11\nplotly\n")
    have = {"PySide6-Essentials": "6.12.0", "numpy": "1.21.0rc1", "plotly": "7.1.0"}
    assert kicad_env.unmet(req, have.get) == ["numpy>=1.24 (has 1.21.0rc1)", "scipy>=1.11"]
    have["numpy"], have["scipy"] = "2.5.4", "1.18.1"
    assert kicad_env.unmet(req, have.get) == []


def test_another_environment_can_be_asked(tmp_path, monkeypatch):
    """The launcher asks KiCad's environment, which it is not running in."""
    req = tmp_path / "req.txt"
    req.write_text("pytest>=1.0\nno-such-package-kipi>=1\n")
    monkeypatch.setattr(kicad_env, "env_python", lambda env: Path(sys.executable))
    assert kicad_env.unmet_in(tmp_path, req) == ["no-such-package-kipi>=1"]
    monkeypatch.setattr(kicad_env, "env_python", lambda env: tmp_path / "no-python")
    assert kicad_env.unmet_in(tmp_path, req) is None


def test_a_full_environment_is_recorded_not_set_up_again(tmp_path):
    env = _env(tmp_path)
    assert plugin_setup.reason(env, REQ, unmet=lambda r: ["numpy>=1.24"]) == plugin_setup.FIRST
    assert plugin_setup.reason(env, REQ, unmet=lambda r: []) is None
    st = state.load(env)
    assert st.tool == kicad_env.ADOPTED and st.requirements_hash == deps.requirements_hash(REQ)
    assert plugin_setup.reason(env, REQ, unmet=lambda r: pytest.fail("asked again")) is None


def test_new_requirements_are_set_up_only_if_unmet(tmp_path):
    env = _env(tmp_path)
    state.save(env, state.State(requirements_hash="old", pardiso=True, tool="pip"))
    assert plugin_setup.reason(env, REQ, unmet=lambda r: ["scipy>=9"]) == plugin_setup.CHANGED
    assert plugin_setup.reason(env, REQ, unmet=lambda r: []) is None
    st = state.load(env)
    assert st.pardiso and st.tool == "pip"                 # what it was set up with stays


def test_adopting_notices_pardiso(tmp_path):
    env = _env(tmp_path)
    site = env / ("Lib/site-packages" if sys.platform == "win32" else "lib/python3.11/site-packages")
    (site / "pypardiso-0.4.7.dist-info").mkdir(parents=True)
    assert kicad_env.adopt(env, "h", "3.11.5").pardiso


def test_pip_runs_as_kicad_runs_it(tmp_path):
    environ = kicad_env.pip_environ(tmp_path, {"PYTHONHOME": "x", "PYTHONPATH": "y", "PATH": "p"})
    assert environ == {"PATH": "p", "VIRTUAL_ENV": str(tmp_path)}


# --- Installing into KiCad's environment -----------------------------------------------------

class FakeInstaller:
    made = []
    fail_with_uv = False

    def __init__(self, python, env, on_line, uv=None, environ=None):
        self.env, self.uv, self.environ, self.calls = env, uv, environ, []
        FakeInstaller.made.append(self)

    tool = property(lambda self: "uv" if self.uv else "pip")

    def create_env(self):
        pytest.fail("KiCad's environment must never be made again")

    def install(self, requirements=None, packages=()):
        if self.uv and FakeInstaller.fail_with_uv:
            raise InstallError("uv: broken")
        self.calls.append(("install", tuple(packages)))

    def uninstall(self, packages):
        self.calls.append(("uninstall", tuple(packages)))


def _summary(env):
    plan = deps.Plan([deps.Package("numpy", "2.5.4", 13_000_000)], [], "live")
    return setup_env.Summary(plugin_setup.FIRST, PY, env, plan, False, "no MKL", False,
                             intro=plugin_setup.intro(plugin_setup.FIRST))


def test_kicads_environment_is_installed_into_not_remade(tmp_path, monkeypatch):
    FakeInstaller.made, FakeInstaller.fail_with_uv = [], True
    monkeypatch.setattr(setup_env, "Installer", FakeInstaller)
    env = _env(tmp_path)
    st = setup_env.perform(_summary(env), False, tmp_path / "KiPI", REQ, lambda line: None,
                           lambda f: None, lambda inst: None, uv=["uv"], owned=False)
    uv_try, pip_try = FakeInstaller.made
    assert uv_try.uv and not pip_try.uv and pip_try.calls == [("install", ())]
    assert pip_try.environ["VIRTUAL_ENV"] == str(env) and "PYTHONHOME" not in pip_try.environ
    assert state.load(env) == st and (env / "pyvenv.cfg").is_file()


def test_kicads_environment_without_python_is_not_made(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_env, "Installer", FakeInstaller)
    with pytest.raises(InstallError, match="Recreate|recreate"):
        setup_env.perform(_summary(tmp_path / "gone"), False, tmp_path, REQ, lambda line: None,
                          lambda f: None, lambda inst: None, uv=None, owned=False)


# --- The plugin's first start ---------------------------------------------------------------

@pytest.fixture
def plugin(tmp_path, monkeypatch):
    """This Python as KiCad's plugin environment, with nothing of KiPI's in it."""
    env = _env(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(env))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "KiCad" / "bin"))
    monkeypatch.setenv("KIPI_HOME", str(tmp_path / "KiPI"))
    monkeypatch.setattr(kicad_env, "unmet", lambda req: ["numpy>=1.24"])
    monkeypatch.setattr(plugin_setup, "reason",
                        lambda env, req, unmet=None: None if state.load(env) else plugin_setup.FIRST)
    calls = []

    def call(cmd, **kw):
        calls.append(cmd)
        state.save(env, state.State(requirements_hash=deps.requirements_hash(REQ)))
        return 0
    monkeypatch.setattr(plugin_setup.subprocess, "call", call)
    return env, calls


def test_the_first_click_runs_the_setup_screen_once(plugin):
    env, calls = plugin
    assert plugin_setup.ensure(lambda text: None)
    assert calls == [[sys.executable, "-m", "kipi.launcher.plugin_setup"]]
    assert plugin_setup.ensure(lambda text: None) and len(calls) == 1
    assert not (env / plugin_setup.LOCK_NAME).exists()


def test_a_canceled_setup_does_not_start_kipi(plugin, monkeypatch):
    monkeypatch.setattr(plugin_setup.subprocess, "call", lambda cmd, **kw: 1)
    assert not plugin_setup.ensure(lambda text: None)


def test_a_second_click_during_setup_does_nothing(plugin):
    env, calls = plugin
    (env / plugin_setup.LOCK_NAME).write_text("")
    assert not plugin_setup.ensure(lambda text: None) and calls == []


def test_a_stale_lock_is_ignored(plugin):
    import os
    env, calls = plugin
    lock = env / plugin_setup.LOCK_NAME
    lock.write_text("")
    old = lock.stat().st_mtime - plugin_setup.LOCK_STALE_S - 10
    os.utime(lock, (old, old))
    assert plugin_setup.ensure(lambda text: None) and len(calls) == 1


def test_a_system_python_is_left_alone(monkeypatch):
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    monkeypatch.setattr(plugin_setup, "reason", lambda *a, **k: pytest.fail("checked"))
    assert plugin_setup.ensure(lambda text: None)


class FakeUI:
    def __init__(self, answer):
        self.answer, self.seen, self.errors = answer, [], []

    def status(self, text):
        pass

    def confirm(self, s):
        self.seen.append(s)
        return self.answer

    def install(self, task):
        return task(lambda line: None, lambda f: None, lambda inst: None)

    def error(self, text):
        self.errors.append(text)

    def close(self):
        pass


def test_the_screen_installs_what_was_chosen(plugin, monkeypatch):
    env, _ = plugin
    monkeypatch.setattr(plugin_setup.deps, "make_plan",
                        lambda python, req, offer: _summary(env).plan)
    done = []

    def perform(summary, choice, data, req, on_line, on_fraction, register, owned=True):
        done.append((choice, owned, summary.env))
        return state.State()
    monkeypatch.setattr(plugin_setup, "perform", perform)
    ui = FakeUI(True)
    assert plugin_setup.main(ui=ui) == 0
    assert done == [(True, False, env)]
    assert "KiCad made a plugin environment" in ui.seen[0].intro_text()
    assert plugin_setup.main(ui=FakeUI(None)) == 1 and len(done) == 1


def test_the_window_toolkit_falls_back(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_wx(name, *a, **k):
        if name.endswith("ui_wx") or name == "wx" or (a and a[2] and "ui_wx" in a[2]):
            raise ImportError("no wx")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_wx)
    monkeypatch.setattr(plugin_setup.system, "can_show_windows", lambda: False)
    monkeypatch.setattr(NativeUI, "find", classmethod(lambda cls, platform=None, which=None: None))
    assert isinstance(plugin_setup.make_ui(lambda text: None), SilentUI)
    monkeypatch.setattr(NativeUI, "find",
                        classmethod(lambda cls, platform=None, which=None: cls("zenity")))
    assert plugin_setup.make_ui(lambda text: None).tool == "zenity"


# --- The desktop's own dialogs --------------------------------------------------------------

def test_the_desktops_dialog_program_is_found():
    have = {"zenity": "/usr/bin/zenity", "kdialog": "/usr/bin/kdialog", "osascript": "/usr/bin/osascript"}
    assert NativeUI.find("darwin", have.get).tool == "osascript"
    assert NativeUI.find("linux", have.get).tool == "zenity"
    assert NativeUI.find("linux", {"kdialog": "x"}.get).tool == "kdialog"
    assert NativeUI.find("win32", have.get) is None
    assert NativeUI.find("linux", {}.get) is None


def _native(tool, returncode=0, stdout=""):
    ran = []

    def run(cmd):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")
    return NativeUI(tool, run=run), ran


def test_macos_asks_with_a_dialog_and_quotes_the_text(tmp_path):
    ui, ran = _native("osascript", stdout="button returned:Install\n")
    s = _summary(tmp_path)
    assert ui.confirm(s) is False                      # yes, and no PARDISO on a Mac
    script = ran[0][2]
    assert script.startswith("display dialog \"") and "default button \"Install\"" in script
    assert ui.ask('a "quoted" \\ path') and '\\"quoted\\" \\\\' in ran[-1][2]
    ui, _ = _native("osascript", returncode=1)
    assert ui.confirm(s) is None


def test_linux_asks_about_pardiso_too(tmp_path):
    ui, ran = _native("zenity")
    s = _summary(tmp_path)
    s.pardiso_offered = True
    assert ui.confirm(s) is True and len(ran) == 2
    assert ran[0][:2] == ["zenity", "--question"] and question_text(s) in ran[0][4]
    ui, ran = _native("kdialog", returncode=1)
    assert ui.confirm(s) is None and ran[0][3] == "--yesno"


def test_with_nothing_to_show_it_installs_without_pardiso(tmp_path):
    said = []
    ui = SilentUI(said.append)
    assert ui.confirm(_summary(tmp_path)) is False and said


# --- Standalone in KiCad's environment ------------------------------------------------------

@pytest.fixture
def in_kicad(tmp_path, monkeypatch):
    """The launcher, with KiPI in a PCM folder whose KiCad environment exists."""
    env = _env(tmp_path)
    root = tmp_path / "KiCad" / "10.0" / "3rdparty" / "plugins" / kicad_env.PCM_FOLDER
    monkeypatch.setattr(paths, "KIPI_ROOT", root)
    monkeypatch.setattr(kicad_env, "plugin_env", lambda version, **kw: env)
    monkeypatch.setattr(launcher.system, "probe", lambda cmd, timeout=20.0: PY)
    monkeypatch.setenv("KIPI_HOME", str(tmp_path / "KiPI"))
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    from kipi.launcher import shortcut
    monkeypatch.setattr(shortcut, "keep_folder_icon", lambda root=None, stamp=None: None)
    monkeypatch.setattr(launcher.system, "pardiso_supported", lambda: (False, "no MKL"))
    monkeypatch.setattr(launcher.deps, "make_plan", lambda python, req, offer: _summary(env).plan)
    got = {"launched": [], "performed": []}

    def perform(summary, choice, data, req, on_line, on_fraction, register, fresh=False,
                owned=True):
        got["performed"].append((summary.env, owned, summary.intro))
        paths.env_python(summary.env).parent.mkdir(parents=True, exist_ok=True)
        paths.env_python(summary.env).write_text("")
        st = state.State(requirements_hash=deps.requirements_hash(req))
        state.save(summary.env, st)
        return st
    monkeypatch.setattr(launcher, "perform", perform)
    monkeypatch.setattr(launcher, "launch",
                        lambda env, board, windowed: got["launched"].append(env) or 0)
    monkeypatch.setattr(launcher, "make_ui", lambda console, windowed: FakeUI(False))
    return env, got


def test_standalone_uses_kicads_environment_and_adds_what_is_missing(in_kicad, monkeypatch):
    env, got = in_kicad
    monkeypatch.setattr(kicad_env, "unmet_in", lambda env, req: ["numpy>=1.24"])
    assert launcher.main([]) == 0
    assert got["performed"][0][:2] == (env, False) and "KiCad made for it" in got["performed"][0][2]
    assert got["launched"] == [env]


def test_standalone_records_a_full_kicad_environment(in_kicad, monkeypatch):
    env, got = in_kicad
    monkeypatch.setattr(kicad_env, "unmet_in", lambda env, req: [])
    assert launcher.main([]) == 0
    assert got["performed"] == [] and got["launched"] == [env]
    assert state.load(env).tool == kicad_env.ADOPTED


def test_own_env_and_an_unsupported_kicad_python_use_kipis_own(in_kicad, monkeypatch, tmp_path):
    env, got = in_kicad
    monkeypatch.setattr(launcher.system, "find_python", lambda commands=None: (PY, []))
    assert launcher.main(["--own-env"]) == 0
    own = paths.env_dir(paths.data_dir())
    assert got["performed"][-1][:2] == (own, True) and got["launched"][-1] == own
    monkeypatch.setattr(launcher.system, "probe",
                        lambda cmd, timeout=20.0: Python("x", (3, 14, 0), True))
    assert launcher.main([]) == 0 and got["launched"][-1] == own


def test_repair_and_reset_leave_kicads_environment_to_kicad(in_kicad):
    env, got = in_kicad
    assert launcher.main(["--reset"]) == 2 and launcher.main(["--repair"]) == 2
    assert env.exists() and got["performed"] == []
