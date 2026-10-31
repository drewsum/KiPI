"""The plugin offers PARDISO on first start, ticked, and remembers an opt-out (#129)."""

import os
import sys

import pytest

pytest.importorskip("qtpy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtCore import QSettings  # noqa: E402
from qtpy.QtWidgets import QApplication, QDialog, QLabel  # noqa: E402

from kipi.gui import pardiso_setup as ps  # noqa: E402
from kipi.launcher.install import Installer  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = QSettings(str(tmp_path / "kipi.ini"), QSettings.IniFormat)
    monkeypatch.setattr(ps, "_settings", lambda: s)
    monkeypatch.setattr(ps, "_env", lambda: tmp_path)        # no setup record unless made
    return s


@pytest.fixture
def here(monkeypatch):
    """A computer that can have PARDISO, without it installed; nothing really runs pip."""
    monkeypatch.setenv("KIPI_DIRECT_SOLVER", "")    # an opt-out sets it; undone after
    monkeypatch.setattr(ps, "available", lambda: (True, ""))
    monkeypatch.setattr(ps, "installed", lambda: False)
    calls = []
    monkeypatch.setattr(ps, "install", lambda parent=None: calls.append("install"))
    monkeypatch.setattr(ps, "uninstall", lambda parent=None: calls.append("uninstall"))
    return calls


def test_offered_only_when_it_can_be_had_and_was_not_turned_down():
    assert ps.should_offer(None, False, True)
    assert ps.should_offer("on", False, True)         # the environment was recreated
    assert not ps.should_offer("off", False, True)
    assert not ps.should_offer(None, True, True)
    assert not ps.should_offer(None, False, False)


def test_not_offered_outside_a_virtual_environment(monkeypatch):
    monkeypatch.setattr(ps, "pardiso_supported", lambda: (True, ""))
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    ok, why = ps.available()
    assert not ok and "environment" in why


def test_dialog_lists_what_it_installs_and_is_ticked(app):
    d = ps.PardisoDialog()
    text = " ".join(lb.text() for lb in d.findChildren(QLabel))
    for p in ps.packages():
        assert p.name in text
    assert d.wanted()


def _answer(monkeypatch, accept: bool, tick: bool):
    def exec_(self):
        self.tick.setChecked(tick)
        return QDialog.Accepted if accept else QDialog.Rejected
    monkeypatch.setattr(ps.PardisoDialog, "exec", exec_)


def test_first_start_installs_when_left_ticked(app, settings, here, monkeypatch):
    _answer(monkeypatch, accept=True, tick=True)
    ps.startup()
    assert here == ["install"] and settings.value(ps.CHOICE_KEY) == "on"


def test_opting_out_is_remembered(app, settings, here, monkeypatch):
    _answer(monkeypatch, accept=True, tick=False)
    ps.startup()
    assert here == [] and settings.value(ps.CHOICE_KEY) == "off"
    monkeypatch.setattr(ps.PardisoDialog, "exec", lambda self: pytest.fail("asked again"))
    ps.startup()


def test_closing_the_dialog_decides_nothing(app, settings, here, monkeypatch):
    _answer(monkeypatch, accept=False, tick=True)
    ps.startup()
    assert here == [] and settings.value(ps.CHOICE_KEY) is None


def test_a_failed_install_is_not_asked_again_every_start(app, settings, here, monkeypatch):
    monkeypatch.setattr(ps, "install", lambda parent=None: "canceled")
    _answer(monkeypatch, accept=True, tick=True)
    ps.startup()
    assert settings.value(ps.CHOICE_KEY) == "off"


def test_removal_waits_for_the_next_start(app, settings, here, monkeypatch):
    monkeypatch.setattr(ps, "installed", lambda: True)
    monkeypatch.setattr(ps.QMessageBox, "information", lambda *a, **k: None)
    _answer(monkeypatch, accept=True, tick=False)
    ps.manage()
    assert here == [] and ps._flag(settings.value(ps.REMOVE_KEY))
    ps.startup()
    assert here == ["uninstall"] and settings.value(ps.REMOVE_KEY) is None


def test_turned_off_but_still_there_is_not_used(app, settings, here, monkeypatch):
    monkeypatch.setattr(ps, "installed", lambda: True)
    settings.setValue(ps.CHOICE_KEY, "off")
    ps.startup()
    assert os.environ.get("KIPI_DIRECT_SOLVER") == "superlu"


def test_startup_never_raises(settings, monkeypatch):
    def boom():
        raise RuntimeError("no")
    monkeypatch.setattr(ps, "available", boom)
    ps.startup()


def test_installer_passes_pip_options_first(tmp_path):
    seen = []
    inst = Installer(None, tmp_path, lambda line: None)
    inst._run = seen.append
    inst.install(packages=["pypardiso"], options=ps.PIP_OPTIONS)
    cmd = seen[0]
    assert cmd[-4:] == ["--no-input", "--only-binary", ":all:", "pypardiso"]


def test_the_setup_screens_answer_decides_and_is_not_asked_again(app, settings, here,
                                                               monkeypatch, tmp_path):
    """KiPI's setup screen asked about PARDISO with everything else (#7)."""
    from kipi.launcher import state
    monkeypatch.setattr(ps.PardisoDialog, "exec", lambda self: pytest.fail("asked again"))
    settings.setValue(ps.CHOICE_KEY, "on")          # an earlier environment's choice
    state.save(tmp_path, state.State(pardiso=False, tool="pip"))
    ps.startup()
    assert settings.value(ps.CHOICE_KEY) == "off" and here == []


def test_an_environment_kicad_filled_is_still_offered(app, settings, here, monkeypatch, tmp_path):
    from kipi.launcher import kicad_env, state
    state.save(tmp_path, state.State(pardiso=False, tool=kicad_env.ADOPTED))
    _answer(monkeypatch, accept=True, tick=True)
    ps.startup()
    assert here == ["install"] and state.load(tmp_path).pardiso


def test_a_choice_in_about_updates_the_setup_record(app, settings, here, monkeypatch, tmp_path):
    from kipi.launcher import state
    state.save(tmp_path, state.State(pardiso=False, tool="pip"))
    _answer(monkeypatch, accept=True, tick=True)
    ps.manage()
    assert here == ["install"] and state.load(tmp_path).pardiso
