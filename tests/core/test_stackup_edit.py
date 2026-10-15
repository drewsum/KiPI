"""Editable stackup thicknesses, kept in the setup and copied back to KiCad (#61)."""

import pytest

from kipi.core.model.board import CopperLayer, DielectricLayer, Stackup
from kipi.core.model.stackup import (
    from_setup, restack, stack_order, summary, thicknesses, to_setup,
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


def test_setup_round_trip_and_mismatch(tmp_path):
    st = four_layer()
    ed = restack(st, [0.07, 0.1, 0.035, 1.0, 0.035, 0.1, 0.07])
    setup = PISetup(stackup=to_setup(ed))
    loaded = PISetup.load(setup.save(tmp_path / "s.kipi-setup.json"))
    back, why = from_setup(st, loaded.stackup)
    assert why == "" and back.edited
    assert thicknesses(back) == pytest.approx(thicknesses(ed))
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
    assert summary(st).startswith("F.Cu 35 µm, FR4 200 µm, In1.Cu 17.5 µm")
    assert "edited" not in summary(st)
    assert summary(restack(st, thicknesses(st))).endswith("(thicknesses edited in KiPI)")


# ------------------------------------------------------------------ back to KiCad
def test_kicad_stackup_takes_the_new_thicknesses():
    pytest.importorskip("kipy")
    from kicad_fake import NM, VERSIONS, FakeKiCad
    from kipy.proto.board import board_pb2 as bp
    from kipi.core.kicad_io.extract import extract_stackup
    from kipi.core.kicad_io.stackup_write import updated_stackup_proto

    fake = FakeKiCad(VERSIONS[-1])
    board = fake.board()
    st = extract_stackup(board)
    original = fake._GetBoardStackup(None).stackup
    # A core of two sublayers: they keep their 1:3 share of the new thickness.
    core = [sl for sl in original.layers if sl.type == bp.BSLT_DIELECTRIC][1]
    core.dielectric.layer[0].thickness.value_nm = int(0.25 * NM)
    extra = core.dielectric.layer.add()
    extra.thickness.value_nm = int(0.75 * NM)
    new = [0.07, 0.1, 0.035, 0.8, 0.035, 0.1, 0.07]
    proto = updated_stackup_proto(original, restack(st, new))
    cu = [sl.thickness.value_nm for sl in proto.layers if sl.type == bp.BSLT_COPPER]
    diel = [sl for sl in proto.layers if sl.type == bp.BSLT_DIELECTRIC]
    assert cu == [70_000, 35_000, 35_000, 70_000]
    assert [d.thickness.value_nm for d in diel] == [100_000, 800_000, 100_000]
    assert [s.thickness.value_nm for s in diel[1].dielectric.layer] == [200_000, 600_000]
    assert original.layers[0].thickness.value_nm == 35_000          # a copy was changed
    with pytest.raises(ValueError):
        short = Stackup(st.copper, st.dielectrics[:2], st.board_thickness_mm)
        updated_stackup_proto(original, short)


def test_kicad_without_the_command_says_so():
    pytest.importorskip("kipy")
    from kicad_fake import VERSIONS, FakeKiCad
    from kipi.core.kicad_io.extract import extract_stackup
    from kipi.core.kicad_io.stackup_write import StackupWriteUnsupported, write_stackup

    fake = FakeKiCad(VERSIONS[-1])
    board = fake.board()
    st = extract_stackup(board)
    with pytest.raises(StackupWriteUnsupported):
        write_stackup(board, restack(st, thicknesses(st)))
    assert fake.calls[-1] == "UpdateBoardStackup"


def test_a_kicad_that_takes_the_stackup_gets_it():
    pytest.importorskip("kipy")
    from kicad_fake import VERSIONS, FakeKiCad
    from kipy.proto.board import board_commands_pb2 as bc
    from kipi.core.kicad_io import stackup_write
    from kipi.core.kicad_io.extract import extract_stackup

    class Future(FakeKiCad):
        sent = None

        def send(self, cmd, response_type):
            if type(cmd).__name__ == "UpdateBoardStackup":
                self.sent = cmd.stackup
                return bc.BoardStackupResponse(stackup=cmd.stackup)
            return super().send(cmd, response_type)

    fake = Future(VERSIONS[-1])
    board = fake.board()
    st = extract_stackup(board)
    stackup_write.write_stackup(board, restack(st, [0.07] + thicknesses(st)[1:]))
    assert fake.sent.layers[0].thickness.value_nm == 70_000


def test_manual_rows_name_every_layer():
    from kipi.core.kicad_io.stackup_write import manual_rows
    rows = manual_rows(four_layer())
    assert rows[0] == ("F.Cu", "0.0350 mm")
    assert rows[1] == ("Dielectric F.Cu – In1.Cu (FR4)", "0.2000 mm")
    assert len(rows) == 7
