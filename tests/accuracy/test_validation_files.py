"""The checked-in validation boards are what make_boards.py builds (#70)."""

import json

import pytest
import shapely
from shapely.geometry import Polygon

from kipi.core.kicad_io.snapshot import to_dict
from validation_boards import boards, open_board


def _same(a, b, where="snapshot"):
    """Equal, with polygons compared as shapes: another GEOS version may start a ring at
    a different vertex or differ in the last digit."""
    if isinstance(a, dict) and set(a) == {"outline", "holes"}:
        pa, pb = (shapely.normalize(Polygon(p["outline"], p["holes"])) for p in (a, b))
        assert pa.equals_exact(pb, 1e-5), where
    elif isinstance(a, dict):
        assert isinstance(b, dict) and set(a) == set(b), where
        for k in a:
            _same(a[k], b[k], f"{where}.{k}")
    elif isinstance(a, list):
        assert isinstance(b, list) and len(a) == len(b), where
        for i, (x, y) in enumerate(zip(a, b)):
            _same(x, y, f"{where}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        assert a == pytest.approx(b, abs=2e-6), where
    else:
        assert a == b, where


@pytest.mark.parametrize("name", sorted(boards.BOARDS))
def test_files_match_the_generator(name):
    snap, setup = open_board(name)
    built_snap, built_setup = boards.BOARDS[name]()
    saved, built = (json.loads(json.dumps(to_dict(s))) for s in (snap, built_snap))
    saved.pop("source"), built.pop("source")
    try:
        _same(saved, built)
    except AssertionError as e:
        rerun = f"rerun python examples/validation/make_boards.py {name}"
        raise AssertionError(f"{e}: {rerun}") from None
    assert json.loads(json.dumps(setup.to_dict())) == json.loads(
        json.dumps(built_setup.to_dict()))
