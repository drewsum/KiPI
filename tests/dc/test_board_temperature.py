# Copyright (c) 2026 Tomachie LLC.
import copy
import sys
from pathlib import Path

import pytest

from kipi.dc.pi.importers.tomachie_pdn import TomachiePdnImporter
from kipi.dc.pi.model import PISetup
from kipi.dc.pi.tree import solve_tree
from kipi.dc.thermal.model import ThermalSettings

sys.path.insert(0, str(Path(__file__).parent))
from test_tomachie_pdn import _load_switch, filtered_board  # noqa: E402
from tree_boards import board, by_name, setup  # noqa: E402

TCR = 0.00393


def _imported(ambient, base=None):
    pdn = _load_switch(datasheet_resistances=[{"spec": "rds_on", "typ": "0.089", "unit": "ohm"}])
    pdn["ambient_max_c"] = ambient
    return TomachiePdnImporter(copy.deepcopy(pdn), filtered_board(), base).run()


def test_every_solve_takes_the_files_ambient():
    s, notes = _imported(85.0)
    assert s.thermal.ambient_c == 85.0 and s.thermal.copper_at_ambient
    assert (s.ac.temperature, s.ac.temperature_c) == ("fixed", 85.0)
    assert any("85 °C" in n for n in notes)


def test_an_unknown_ambient_keeps_the_setups_temperatures():
    s, notes = _imported("unknown")
    assert not s.thermal.copper_at_ambient and s.ac.temperature == PISetup().ac.temperature
    assert any("no maximum ambient" in n for n in notes)


def test_a_setup_imported_onto_takes_the_ambient_and_keeps_thermal_coupling():
    base = PISetup()
    base.ac.temperature = "thermal"
    s, _ = _imported(70.0, base)
    assert (s.ac.temperature, s.ac.temperature_c) == ("thermal", 70.0)
    assert s.thermal.ambient_c == 70.0
    assert base.thermal.ambient_c == ThermalSettings().ambient_c


def test_copper_at_ambient_without_cosimulation():
    snap = board()
    s = setup("ldo")
    cold = solve_tree(snap, s)
    s.thermal = ThermalSettings(ambient_c=85.0, copper_at_ambient=True)
    warm = solve_tree(snap, s)
    ratio = by_name(warm.loads, "U2").drop / by_name(cold.loads, "U2").drop
    assert ratio == pytest.approx(1 + TCR * 65.0, rel=1e-3)
