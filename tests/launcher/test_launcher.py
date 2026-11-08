"""The standalone launcher's groundwork: where it installs, which Python it uses, what it
will install and how big that is, and how it knows an install is done."""

import json
import sys
import textwrap
from pathlib import Path

import pytest

from kipi.launcher import deps, paths, state, system
from kipi.launcher.system import Python

ROOT = Path(__file__).resolve().parents[2]


# --- Where -------------------------------------------------------------------------------

def test_each_platform_keeps_kipi_in_its_usual_place(tmp_path):
    home = tmp_path
    assert paths.data_dir({"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}, "win32", home) == \
        Path(r"C:\Users\me\AppData\Local") / "KiPI"
    assert paths.data_dir({}, "darwin", home) == home / "Library/Application Support/KiPI"
    assert paths.data_dir({}, "linux", home) == home / ".local/share/kipi"
    assert paths.data_dir({"XDG_DATA_HOME": "/data"}, "linux", home) == Path("/data/kipi")
    assert paths.data_dir({"KIPI_HOME": "/elsewhere", "LOCALAPPDATA": "x"}, "win32", home) == \
        Path("/elsewhere")


def test_the_environment_python(tmp_path):
    assert paths.env_python(tmp_path, platform="win32") == tmp_path / "Scripts/python.exe"
    assert paths.env_python(tmp_path, True, "win32") == tmp_path / "Scripts/pythonw.exe"
    assert paths.env_python(tmp_path, True, "linux") == tmp_path / "bin/python"


# --- Which Python ------------------------------------------------------------------------

def test_supported_pythons():
    assert system.is_supported((3, 10, 0)) and system.is_supported((3, 13, 9))
    assert not system.is_supported((3, 9, 18)) and not system.is_supported((3, 14, 0))


def test_the_first_python_that_can_make_an_environment_wins():
    found = {
        "old": Python("/usr/bin/python3.9", (3, 9, 2), True),
        "new": Python("/usr/bin/python3.14", (3, 14, 0), True),
        "novenv": Python("/usr/bin/python3.12", (3, 12, 3), False),
        "good": Python("/opt/python3.11", (3, 11, 9), True),
        "again": Python("/opt/python3.11", (3, 11, 9), True),
    }
    p, problems = system.find_python([["old"], ["missing"], ["new"], ["novenv"], ["good"]],
                                     lambda cmd: found.get(cmd[0]))
    assert p == found["good"]
    assert len(problems) == 3 and "python3-venv" in problems[2]
    p, problems = system.find_python([["old"]], lambda cmd: found.get(cmd[0]))
    assert p is None and problems == ["/usr/bin/python3.9 is Python 3.9.2"]


def test_windows_asks_the_py_launcher_newest_first(monkeypatch):
    cmds = system.candidates("win32", lambda name: r"C:\Windows\py.exe" if name == "py" else None)
    assert cmds[0] == [sys.executable]
    assert cmds[1:] == [[r"C:\Windows\py.exe", f"-3.{m}"] for m in (13, 12, 11, 10)]


def test_macos_looks_where_python_installers_put_it():
    """KiCad started from the Dock has launchd's PATH, with no Homebrew or python.org in it
    (#237)."""
    dirs = system.extra_dirs("darwin", home="/Users/a")
    assert dirs[:3] == ["/opt/homebrew/bin", "/usr/local/bin", "/Users/a/.local/bin"]
    assert "/Library/Frameworks/Python.framework/Versions/3.12/bin" in dirs
    assert "/Library/Frameworks/Python.framework/Versions/3.9/bin" not in dirs
    assert system.extra_dirs("win32") == []
    brew = {"python3.11": "/opt/homebrew/bin/python3.11"}
    assert system.candidates("darwin", brew.get)[1:] == [["/opt/homebrew/bin/python3.11"]]


def test_a_real_python_is_probed():
    p = system.probe([sys.executable])
    assert p is not None and p.version[:2] == tuple(sys.version_info[:2])


@pytest.mark.parametrize("os_name, machine, ok", [
    ("Windows", "AMD64", True), ("Linux", "x86_64", True), ("Darwin", "x86_64", False),
    ("Darwin", "arm64", False), ("Linux", "aarch64", False), ("Windows", "ARM64", False),
])
def test_pardiso_only_where_mkl_is(os_name, machine, ok):
    supported, why = system.pardiso_supported(os_name, machine)
    assert supported is ok and (why == "") is ok


@pytest.mark.parametrize("os_name, machine, key", [
    ("Windows", "AMD64", "win_amd64"), ("Linux", "x86_64", "linux_x86_64"),
    ("Linux", "aarch64", "linux_aarch64"), ("Darwin", "arm64", "macos_arm64"),
    ("Darwin", "x86_64", "macos_x86_64"),
])
def test_platform_keys(os_name, machine, key):
    assert deps.platform_key(os_name, machine) == key


# --- What, and how big --------------------------------------------------------------------

def test_requirements_hash_ignores_comments_and_spacing(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("numpy>=1.24\nscipy >= 1.11\n")
    b.write_text("# KiPI\nscipy>=1.11\n\nnumpy>=1.24  # arrays\n")
    assert deps.requirements_hash(a) == deps.requirements_hash(b)
    b.write_text("scipy>=1.12\nnumpy>=1.24\n")
    assert deps.requirements_hash(a) != deps.requirements_hash(b)


def test_totals_are_unknown_if_any_size_is():
    plan = deps.Plan([deps.Package("a", "1", 1_000_000), deps.Package("b", "1", 3_000_000)],
                     [deps.Package("mkl", "1", 200_000_000)], "live")
    assert plan.download(False) == 4_000_000 and plan.download(True) == 204_000_000
    assert plan.installed(False) == int(4_000_000 * deps.INSTALLED_PER_DOWNLOAD)
    plan.packages[1].size = None
    assert plan.download(False) is None and plan.installed(True) is None
    assert deps.fmt_size(None) == "size unknown"
    assert deps.fmt_size(254_000_000) == "254 MB" and deps.fmt_size(1_500_000_000) == "1.5 GB"


FAKE_PIP = textwrap.dedent('''
    import json, sys
    args = sys.argv[1:]
    assert args[:4] == ["-m", "pip", "install", "--dry-run"], args
    report = args[args.index("--report") + 1]
    install = [{"metadata": {"name": "numpy", "version": "2.5.4"},
                "download_info": {"url": "https://files.example/numpy.whl"}}]
    if "pypardiso" in args:
        install.append({"metadata": {"name": "mkl", "version": "2026.2.0"},
                        "download_info": {"url": "https://files.example/mkl.whl"}})
    json.dump({"install": install}, open(report, "w"))
''')


def test_resolve_reads_pips_report(tmp_path, monkeypatch):
    script = tmp_path / "fake_python.py"
    script.write_text(FAKE_PIP)
    req = tmp_path / "req.txt"
    req.write_text("numpy\n")
    pkgs = deps.resolve([sys.executable, str(script)], req)
    assert [(p.name, p.version) for p in pkgs] == [("numpy", "2.5.4")]

    monkeypatch.setattr(deps, "_head_size", lambda url, timeout: 7 if "numpy" in url else 9)
    plan = deps.live_plan([sys.executable, str(script)], req, offer_pardiso=True)
    assert plan.source == "live" and [p.name for p in plan.pardiso] == ["mkl"]
    assert plan.download(True) == 16


def test_resolve_says_what_pip_said(tmp_path):
    script = tmp_path / "failing.py"
    script.write_text("import sys; print('ERROR: No matching distribution found for x', "
                      "file=sys.stderr); sys.exit(1)")
    with pytest.raises(deps.ResolveError, match="No matching distribution"):
        deps.resolve([sys.executable, str(script)], tmp_path / "req.txt")


def test_offline_falls_back_to_the_release_list_then_names(monkeypatch):
    def offline(*a, **k):
        raise deps.ResolveError("no network")
    monkeypatch.setattr(deps, "live_plan", offline)
    manifest = {"kipi": "0.8.0", "platforms": {"win_amd64": {
        "packages": [["numpy", "2.5.4", 13_000_000]], "pardiso": [["mkl", "2026", 1]]}}}
    monkeypatch.setattr(deps, "load_manifest", lambda path=deps.MANIFEST: manifest)
    plan = deps.make_plan(["python"], paths.requirements_file(ROOT), True, "win_amd64")
    assert plan.source == "manifest" and "no network" in plan.note and "0.8.0" in plan.note
    assert plan.download(True) == 13_000_001
    plan = deps.make_plan(["python"], paths.requirements_file(ROOT), False, "win_amd64")
    assert plan.pardiso == []
    plan = deps.make_plan(["python"], paths.requirements_file(ROOT), False, "linux_aarch64")
    assert plan.source == "" and "PySide6-Essentials" in [p.name for p in plan.packages]
    assert plan.download(False) is None


def test_the_release_list_matches_the_requirements():
    """Regenerate it with .github/scripts/dep_sizes.py after changing the requirements."""
    data = deps.load_manifest()
    assert data, "kipi/launcher/deps_manifest.json is missing"
    assert data["requirements_hash"] == deps.requirements_hash(paths.requirements_file(ROOT))
    for key in ("win_amd64", "linux_x86_64", "macos_arm64"):
        entry = data["platforms"][key]
        assert entry["packages"] and all(size for _n, _v, size in entry["packages"])
    assert data["platforms"]["win_amd64"]["pardiso"]
    assert not data["platforms"]["macos_arm64"]["pardiso"]


# --- Done or not --------------------------------------------------------------------------

def test_setup_is_needed_until_an_install_finishes(tmp_path):
    env = tmp_path / "venv"
    assert "not set up" in state.needs_setup(env, "h1")
    py = paths.env_python(env)
    py.parent.mkdir(parents=True)
    py.write_text("")
    assert "did not finish" in state.needs_setup(env, "h1")
    state.save(env, state.State(requirements_hash="h1", python="3.13.1"))
    assert state.needs_setup(env, "h1") is None
    assert "different packages" in state.needs_setup(env, "h2")
    assert state.load(env).python == "3.13.1"
    py.unlink()
    assert "incomplete" in state.needs_setup(env, "h1")


def test_a_record_from_a_newer_launcher_still_reads(tmp_path):
    env = tmp_path / "venv"
    env.mkdir()
    paths.state_path(env).write_text(json.dumps({"requirements_hash": "h", "pardiso": True,
                                                 "something_new": 1}))
    s = state.load(env)
    assert s.requirements_hash == "h" and s.pardiso
