"""KiPI refuses to connect while more than one KiCad is open."""

import pytest

from kipi.core.kicad_io import extract, instances


def test_recognizes_kicad_executables_on_every_platform():
    for name in ("kicad.exe", "KiCad.exe", "pcbnew.exe", "kicad", "pcbnew",
                 "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad"):
        assert instances.is_kicad(name), name
    for name in ("kicad-cli.exe", "kicad-cli", "eeschema", "python.exe", "kicad.py", ""):
        assert not instances.is_kicad(name), name


def test_lists_this_machines_processes():
    found = instances.running_kicads()
    assert found is not None
    assert all(instances.is_kicad(name) for _pid, name in found)


@pytest.mark.parametrize("procs, blocked", [
    ([], False),
    ([(10, "kicad.exe"), (11, "python.exe")], False),
    ([(10, "kicad.exe"), (12, "kicad.exe")], True),
    ([(10, "kicad"), (12, "pcbnew")], True),
])
def test_more_than_one_kicad_blocks(monkeypatch, procs, blocked):
    monkeypatch.delenv(instances.ALLOW_ENV, raising=False)
    monkeypatch.setattr(instances, "running_kicads", lambda: instances.kicads_in(procs))
    msg = instances.multiple_kicads_message()
    assert bool(msg) is blocked
    if blocked:
        assert "PID 10" in msg and "PID 12" in msg
        with pytest.raises(extract.ExtractionError, match="one KiCad at a time"):
            extract.connect(retries=1)


def test_unknown_process_list_does_not_block(monkeypatch):
    monkeypatch.setattr(instances, "running_kicads", lambda: None)
    assert instances.multiple_kicads_message() is None


def test_a_failing_process_list_is_unknown(monkeypatch):
    def boom():
        raise OSError("denied")
    for fn in ("_windows_processes", "_proc_processes", "_ps_processes"):
        monkeypatch.setattr(instances, fn, boom)
    assert instances.running_kicads() is None


def test_environment_override(monkeypatch):
    monkeypatch.setattr(instances, "running_kicads",
                        lambda: [(1, "kicad.exe"), (2, "kicad.exe")])
    monkeypatch.setenv(instances.ALLOW_ENV, "1")
    assert instances.multiple_kicads_message() is None
