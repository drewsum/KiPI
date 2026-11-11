# Copyright (c) 2026 Tomachie LLC.
"""Where a capacitor model's values came from survives its setup file and its model file."""

from kipi.ac import cap_file
from kipi.ac.model import CapModel
from kipi.core.model.spice import SpiceRef
from kipi.dc.pi.model import cap_from_dict


def _model() -> CapModel:
    return CapModel("GRM188", 1e-6, 5e-10, 0.01, ["C1"],
                    spice=SpiceRef("GRM188", "* text"),
                    values_from={"C": "datasheet", "ESR": "estimated"},
                    defaults_because="no_part_number",
                    equivalent="TDK C1608X7R1E104K080AA", doubt="above the family's measured ESR")


def _sources(m: CapModel) -> tuple:
    return m.values_from, m.defaults_because, m.equivalent, m.doubt


def test_setup_data_keeps_sources_and_spice():
    m = _model()
    back = cap_from_dict({"name": m.name, "spice": {"subckt": "GRM188", "netlist": "* text"},
                          "values_from": m.values_from, "defaults_because": m.defaults_because,
                          "equivalent": m.equivalent, "doubt": m.doubt})
    assert _sources(back) == _sources(m)
    assert isinstance(back.spice, SpiceRef) and back.spice.subckt == "GRM188"


def test_model_file_keeps_sources_and_spice():
    m = _model()
    back = cap_file.from_dict(cap_file.to_dict(m))
    assert _sources(back) == _sources(m)
    assert isinstance(back.spice, SpiceRef) and back.spice.subckt == "GRM188"
    assert back.refs == []
