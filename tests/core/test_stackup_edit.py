"""Editable stackup thicknesses and εr, kept in the setup (#61)."""

import pytest

from kipi.core.model.board import CopperLayer, DielectricLayer, Stackup
from kipi.core.model.stackup import (
    dielectric_er, from_setup, restack, stack_order, summary, thicknesses, to_setup,
)
from kipi.dc.pi.model import PISetup


def four_layer() -> Stackup:
    """35/17.5/17.5/35 µm copper over 0.2/1.065/0.2 mm dielectrics, as KiPI reads it."""
    cu_t = [0.035, 0.0175, 0.0175, 0.035]
    diel_t = [0.2, 1.065, 0.2]
    total = sum(cu_t) + sum(diel_t)
    z = total
    copper, dielectrics = [], []
    for i, (lid, name) in enumerate(((3, "F.Cu"), (4, "In1.Cu"), (6, "In2.Cu"), (34, "B.Cu"))):
        z -= cu_t[i]
        copper.append(CopperLayer(lid, name, i, cu_t[i], z))
        if i < 3:
            z -= diel_t[i]
            dielectrics.append(DielectricLayer(diel_t[i], "FR4", 4.4, lid, -1))
    for d, below in zip(dielectrics, copper[1:]):
        d.below_layer = below.id
    return Stackup(copper, dielectrics, total)


def test_stack_order_interleaves_copper_and_dielectrics():
    st = four_layer()
    kinds = ["cu" if isinstance(e, CopperLayer) else "d" for e in stack_order(st)]
    assert kinds == ["cu", "d", "cu", "d", "cu", "d", "cu"]
    assert thicknesses(st) == pytest.approx([0.035, 0.2, 0.0175, 1.065, 0.0175, 0.2, 0.035])


def test_restack_moves_every_layer_and_the_board():
    st = four_layer()
    new = [0.070, 0.1, 0.035, 1.0, 0.035, 0.1, 0.070]          # 2 oz outer, thinner prepreg
    ed = restack(st, new)
    assert ed.edited and not st.edited
    assert ed.board_thickness_mm == pytest.approx(sum(new))
    assert [c.thickness_mm for c in ed.copper] == pytest.approx([0.070, 0.035, 0.035, 0.070])
    assert [d.thickness_mm for d in ed.dielectrics] == pytest.approx([0.1, 1.0, 0.1])
    assert ed.copper[0].z_top_mm == pytest.approx(ed.board_thickness_mm)
    assert ed.copper[-1].z_bottom_mm == pytest.approx(0.0)
    assert ed.copper[1].z_top_mm == pytest.approx(ed.board_thickness_mm - 0.070 - 0.1)
    # Names, ids, materials and εr are kept; the original is untouched.
    assert [(c.id, c.name) for c in ed.copper] == [(c.id, c.name) for c in st.copper]
    assert ed.dielectrics[1].epsilon_r == 4.4
    assert st.copper[0].thickness_mm == 0.035
    with pytest.raises(ValueError):
        restack(st, new[:-1])


def test_restack_sets_dielectric_er():
    st = four_layer()
    ed = restack(st, thicknesses(st), [3.5, 4.6, 0.0])
    assert dielectric_er(ed) == [3.5, 4.6, 0.0]
    assert dielectric_er(st) == [4.4, 4.4, 4.4]
    assert dielectric_er(restack(st, thicknesses(st))) == [4.4, 4.4, 4.4]   # kept
    with pytest.raises(ValueError):
        restack(st, thicknesses(st), [4.0])


def test_setup_round_trip_and_mismatch(tmp_path):
    st = four_layer()
    ed = restack(st, [0.07, 0.1, 0.035, 1.0, 0.035, 0.1, 0.07], [3.9, 4.6, 3.9])
    setup = PISetup(stackup=to_setup(ed))
    loaded = PISetup.load(setup.save(tmp_path / "s.kipi-setup.json"))
    back, why = from_setup(st, loaded.stackup)
    assert why == "" and back.edited
    assert thicknesses(back) == pytest.approx(thicknesses(ed))
    assert dielectric_er(back) == [3.9, 4.6, 3.9]
    older = to_setup(ed)                    # saved before εr could be edited
    del older["dielectric_er"]
    assert dielectric_er(from_setup(st, older)[0]) == [4.4, 4.4, 4.4]
    assert from_setup(st, None) == (None, "")
    assert PISetup.from_dict({}).stackup is None
    # A layer renamed in KiCad since: the saved thicknesses no longer apply.
    renamed = to_setup(ed)
    renamed["copper_mm"]["GND"] = renamed["copper_mm"].pop("In1.Cu")
    back, why = from_setup(st, renamed)
    assert back is None and "4 copper" in why
    fewer = to_setup(ed)
    fewer["dielectric_mm"].pop()
    assert from_setup(st, fewer)[0] is None


def test_summary_marks_an_edited_stackup():
    st = four_layer()
    assert summary(st).startswith("F.Cu 35 µm, FR4 200 µm εr 4.4, In1.Cu 17.5 µm")
    assert "edited" not in summary(st)
    assert summary(restack(st, thicknesses(st))).endswith("(edited in KiPI)")


def test_dielectric_materials_are_edited_and_kept():
    from kipi.core.model.stackup import dielectric_materials
    st = four_layer()
    ed = restack(st, thicknesses(st), [3.48, 4.4, 4.4],
                 materials=["Rogers RO4350B", "FR4", "FR4"])
    assert dielectric_materials(ed) == ["Rogers RO4350B", "FR4", "FR4"]
    assert dielectric_materials(st) == ["FR4", "FR4", "FR4"]
    back, _ = from_setup(st, to_setup(ed))
    assert dielectric_materials(back) == ["Rogers RO4350B", "FR4", "FR4"]
    assert "Rogers RO4350B 200 µm εr 3.48" in summary(ed)
    with pytest.raises(ValueError):
        restack(st, thicknesses(st), materials=["FR4"])
