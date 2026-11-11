# Copyright (c) 2026 Tomachie LLC.
"""A headless solve ends cleanly: ``python -m kipi --solve`` run as a child process exits
with its result, leaves no process behind, no temporary files, and the same when it fails
or is killed. On a shared server a solve that lingers starves everything else."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from kipi.__main__ import runs_headless
from kipi.core.model.snapshot_io import save
from test_pdn_results import _pdn, filtered_board

REPO = Path(__file__).resolve().parents[2]
TIMEOUT_S = 300


def _inputs(tmp_path, pdn=None):
    board = tmp_path / "board.json.gz"
    save(filtered_board(), board)
    path = tmp_path / "board.pdn.json"
    path.write_text(json.dumps(pdn if pdn is not None else _pdn()), encoding="utf-8")
    return board, path


def _command(board, pdn, out, *extra):
    return [sys.executable, "-m", "kipi", str(board), "--pdn-json", str(pdn),
            "--solve", str(out), "--threads", "1", "--memory-gb", "2", *extra]


def _env():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), env.get("PYTHONPATH", "")])
    return env


def _children_of(pid: int):
    """Processes whose parent is ``pid`` (still running after it ended: orphans)."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}' | "
             "ForEach-Object { \"$($_.ProcessId) $($_.Name)\" }"],
            capture_output=True, text=True, timeout=60)
    else:
        out = subprocess.run(["ps", "-o", "pid=,comm=", "--ppid", str(pid)],
                             capture_output=True, text=True, timeout=60)
    return [line for line in out.stdout.splitlines() if line.strip()]


def _temp_entries():
    return {p.name for p in Path(tempfile.gettempdir()).iterdir()
            if p.name.lower().startswith(("kipi", "tmp"))}


def test_a_run_with_no_window_is_not_relaunched():
    assert runs_headless(["board.tgz", "--pdn-json", "p.json", "--solve", "out"])
    assert runs_headless(["--dump=out.json.gz"])
    assert not runs_headless(["board.tgz", "--pdn-json", "p.json", "--background"])


def test_a_solve_ends_and_leaves_nothing_behind(tmp_path):
    board, pdn = _inputs(tmp_path)
    out = tmp_path / "out"
    before = _temp_entries()
    t0 = time.monotonic()
    proc = subprocess.Popen(_command(board, pdn, out, "--ac", "all"), cwd=REPO,
                            env=_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    log, _ = proc.communicate(timeout=TIMEOUT_S)
    assert proc.returncode == 0, log
    # The result came from this process, not a relaunched copy that is still at work.
    assert "Wrote" in log and (out / "pdn_results.json").is_file()
    assert time.monotonic() - t0 < TIMEOUT_S
    assert _children_of(proc.pid) == []
    names = {p.name for p in out.iterdir()}
    assert names - {"pdn_results.json"} <= {n for n in names if n.startswith("ac_")
                                            and n.endswith(".html")}
    assert not list(out.glob("*.tmp"))
    assert _temp_entries() - before == set()


def test_a_failed_solve_ends_with_an_error(tmp_path):
    board, _ = _inputs(tmp_path)
    proc = subprocess.run(_command(board, tmp_path / "missing.pdn.json", tmp_path / "out"),
                          cwd=REPO, env=_env(), capture_output=True, text=True,
                          timeout=TIMEOUT_S)
    assert proc.returncode != 0
    assert not (tmp_path / "out" / "pdn_results.json").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree")
def test_a_killed_solve_leaves_no_process(tmp_path):
    board, pdn = _inputs(tmp_path)
    proc = subprocess.Popen(_command(board, pdn, tmp_path / "out", "--ac", "all"),
                            cwd=REPO, env=_env(), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    time.sleep(3)
    proc.kill()
    proc.wait(timeout=60)
    assert _children_of(proc.pid) == []
    assert not list((tmp_path / "out").glob("*.tmp")) if (tmp_path / "out").exists() \
        else True
