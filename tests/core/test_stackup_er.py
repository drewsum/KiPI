"""The dielectric constant comes from KiCad's stackup (#65)."""

import pytest

pytest.importorskip("kipy")

from kipy.errors import ApiError  # noqa: E402
from kipy.proto.common.envelope_pb2 import ApiStatusCode  # noqa: E402

from kicad_fake import NM, VERSIONS, FakeKiCad  # noqa: E402
from kipi.core.kicad_io.extract import combined_epsilon_r, extract_board  # noqa: E402


@pytest.mark.parametrize("subs, er", [
    ([(1.0, 4.6)], 4.6),
    ([(0.1, 4.0), (0.1, 5.0)], 0.2 / (0.1 / 4.0 + 0.1 / 5.0)),      # in series, not 4.0
    ([(0.2, 4.3), (0.1, 0.0)], 4.3),                                 # unknown left out
    ([(0.0, 3.9), (0.0, 4.4)], 3.9),                                 # no thicknesses
    ([(0.2, 0.0)], 0.0),
    ([], 0.0),
])
def test_combined_epsilon_r(subs, er):
    assert combined_epsilon_r(subs) == pytest.approx(er)


class SplitCore(FakeKiCad):
    """The core is two sublayers of different εr, as KiCad allows."""

    def _GetBoardStackup(self, cmd):
        r = super()._GetBoardStackup(cmd)
        core = [sl for sl in r.stackup.layers if sl.dielectric.layer
                and sl.dielectric.layer[0].material_name == "Core FR4"][0]
        first = core.dielectric.layer[0]
        first.thickness.value_nm = int(0.5 * NM)
        second = core.dielectric.layer.add()
        second.material_name, second.epsilon_r = "Core FR4", 3.6
        second.thickness.value_nm = int(0.565 * NM)
        return r


class NoStackup(FakeKiCad):
    def _GetBoardStackup(self, cmd):
        raise ApiError("no stackup", code=ApiStatusCode.AS_BAD_REQUEST)


def test_every_sublayer_counts():
    snap = extract_board(SplitCore(VERSIONS[-1]).board())
    core = snap.stackup.dielectrics[1]
    assert core.thickness_mm == pytest.approx(1.065)
    assert core.epsilon_r == pytest.approx(1.065 / (0.5 / 4.6 + 0.565 / 3.6))


def test_default_stackup_leaves_er_to_the_ac_setting():
    snap = extract_board(NoStackup(VERSIONS[-1]).board())
    assert not snap.stackup.from_board
    assert snap.stackup.dielectrics
    assert all(d.epsilon_r == 0.0 for d in snap.stackup.dielectrics)
