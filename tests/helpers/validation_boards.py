"""The validation boards in examples/validation: their snapshots, setups and dimensions."""

import importlib.util
from pathlib import Path

from kipi.core.kicad_io.snapshot import load
from kipi.dc.pi.model import PISetup

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "validation_boards", ROOT / "examples" / "validation" / "make_boards.py")
boards = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(boards)


def open_board(name):
    """The checked-in snapshot and setup of validation board ``name``."""
    snap_path, setup_path = boards.paths(name)
    return load(snap_path), PISetup.load(setup_path)
