"""Part 3D models in the geometry view: which parts get one, and how they are drawn."""

import os
from types import MethodType
from types import SimpleNamespace as NS

import numpy as np
import pytest

pv = pytest.importorskip("pyvista")
pytest.importorskip("pyvistaqt")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy.QtWidgets import QApplication  # noqa: E402

from kipi.gui.viewer3d import Viewer3D, _merge_models  # noqa: E402
from phase_boards import board, two_phase  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _part(z0=1.6, normals=True):
    mesh = pv.Box(bounds=(0, 1, 0, 1, z0, z0 + 1)).triangulate()
    mesh.clear_data()
    mesh.cell_data["rgb"] = np.full((mesh.n_cells, 3), 200, np.uint8)
    if normals:
        mesh.point_data["Normals"] = np.tile([0.0, 0.0, 1.0], (mesh.n_points, 1))
    return mesh


def _view(zscale=1.0):
    v = NS(plotter=pv.Plotter(off_screen=True), _models={}, _model_actors={}, _board_top=0.0,
           _model_board=0.0,
           _zscale=zscale, _show_models=True, _show_models_result=False, _show_board=True,
           _show_vias=True, _show_pi=True, _pi_markers=[], _pi_actors={},
           _result=None, _result_field="voltage", _chunk_actors={}, _result_actors={},
           _arrow_actors={}, _result_via_actor=None, _board_actors=[], _hidden_layers=set(),
           _coating_actors=[], _show_mask=False, _show_silk=False,
           _show_mask_result=False, _show_silk_result=False, _coupling_actors=[],
           _coupling_bar=None, _coupling_title=None)
    for name in ("set_models", "_remove_model_actors", "_place_models", "_apply_visibility",
                 "set_zscale"):
        setattr(v, name, MethodType(getattr(Viewer3D, name), v))
    return v


def _actors(v):
    return list(v.plotter.renderer.GetActors())


def test_models_are_drawn_per_side_and_never_picked():
    v = _view()
    top, bottom = _part(), _part(z0=-1.0)
    v.set_models({"C1": (top, "top"), "C2": (top, "top"), "C3": (bottom, "bottom")}, 1.6)
    assert set(v._model_actors) == {"top", "bottom"}
    assert all(a in _actors(v) for a in v._model_actors.values())
    assert not any(a.GetPickable() for a in v._model_actors.values())
    assert v._model_actors["top"].GetMapper().GetInput().n_cells == 2 * top.n_cells

    same = dict(v._model_actors)
    v.set_models({"C1": (top, "top"), "C2": (top, "top"), "C3": (bottom, "bottom")}, 1.6)
    assert v._model_actors == same                        # nothing changed: not rebuilt

    v.set_models({"C3": (bottom, "bottom")}, 1.6)
    assert set(v._model_actors) == {"bottom"}
    assert same["top"] not in _actors(v)
    v.set_models({}, 1.6)
    assert v._model_actors == {} and not _actors(v)


def test_top_parts_sit_on_the_stretched_board_unstretched():
    v = _view(zscale=5.0)
    v.set_models({"C1": (_part(), "top"), "C3": (_part(z0=-1.0), "bottom")}, 1.6)
    top, bottom = v._model_actors["top"], v._model_actors["bottom"]
    assert top.GetPosition()[2] == pytest.approx(1.6 * 4)
    assert top.GetScale() == (1.0, 1.0, 1.0)
    assert bottom.GetPosition() == (0.0, 0.0, 0.0)        # z = 0 is the board's bottom
    v.set_zscale(2.0)
    assert top.GetPosition()[2] == pytest.approx(1.6)
    assert top.GetScale() == (1.0, 1.0, 1.0)


def test_top_parts_follow_a_board_made_thicker_in_the_stackup_pane():
    v = _view(zscale=2.0)
    # The models were placed by kicad-cli on KiCad's 1.6 mm board; the edit makes it 2.0.
    v.set_models({"C1": (_part(), "top"), "C3": (_part(z0=-1.0), "bottom")}, 2.0, 1.6)
    assert v._model_actors["top"].GetPosition()[2] == pytest.approx(2.0 * 2 - 1.6)
    assert v._model_actors["bottom"].GetPosition() == (0.0, 0.0, 0.0)


def test_models_are_off_over_results_until_asked_for():
    v = _view()
    v.set_feature_visible = MethodType(Viewer3D.set_feature_visible, v)
    v.set_models({"C1": (_part(), "top")}, 1.6)
    actor = v._model_actors["top"]
    assert actor.GetVisibility()
    v.set_feature_visible("models", False)
    assert not actor.GetVisibility()
    v.set_feature_visible("models", True)
    v._result = object()                                  # a result is shown
    v._apply_visibility()
    assert not actor.GetVisibility()                      # off by default over results
    v.set_feature_visible("models", True)
    assert actor.GetVisibility() and v._show_models_result
    v.set_feature_visible("models", False)
    assert not actor.GetVisibility() and v._show_models   # the copper view's is kept
    v._result = None
    v._apply_visibility()
    assert actor.GetVisibility()


def test_the_checkbox_follows_the_view(app, no_3d, monkeypatch):
    w, shown, asked, _ = _window(no_3d, monkeypatch)
    assert w.chk_models.isChecked()
    w.viewer.models_shown = False                         # the result view's setting
    w.viewer.result_shown.emit(True)
    assert not w.chk_models.isChecked()
    w.viewer.models_shown = True                          # back to the copper view's
    w.viewer.result_shown.emit(False)
    assert w.chk_models.isChecked()
    w.close()


def test_mask_and_silk_are_off_over_results_until_asked_for():
    """#144: the copper view's Mask and Silkscreen settings do not carry over results."""
    v = _view()
    v.set_feature_visible = MethodType(Viewer3D.set_feature_visible, v)
    v.feature_shown = MethodType(Viewer3D.feature_shown, v)
    mask, silk = pv.Plotter(off_screen=True).add_mesh(_part()), \
        pv.Plotter(off_screen=True).add_mesh(_part())
    v._coating_actors = [("mask", mask), ("silk", silk)]
    v.set_feature_visible("mask", True)
    v.set_feature_visible("silk", True)
    assert mask.GetVisibility() and silk.GetVisibility()
    v._result = object()                                  # a result is shown
    v._apply_visibility()
    assert not mask.GetVisibility() and not silk.GetVisibility()
    assert not v.feature_shown("mask") and not v.feature_shown("silk")
    v.set_feature_visible("silk", True)
    assert silk.GetVisibility() and not mask.GetVisibility()
    v._result = None                                      # the copper view's are kept
    v._apply_visibility()
    assert mask.GetVisibility() and silk.GetVisibility() and v.feature_shown("mask")


def test_mask_and_silk_checkboxes_follow_the_view(app, no_3d, monkeypatch):
    w, *_ = _window(no_3d, monkeypatch)
    w.chk_mask.setChecked(True)
    w.chk_silk.setChecked(True)
    w.viewer.feature_shown = lambda f: False              # the result view's settings
    w.viewer.result_shown.emit(True)
    assert not w.chk_mask.isChecked() and not w.chk_silk.isChecked()
    w.viewer.feature_shown = lambda f: True               # back to the copper view's
    w.viewer.result_shown.emit(False)
    assert w.chk_mask.isChecked() and w.chk_silk.isChecked()
    w.close()


def test_merged_models_keep_colors_and_normals_only_if_all_have_them():
    a, b = _part(), _part(z0=5.0, normals=False)
    b.cell_data["rgb"][:] = 7
    both = _merge_models([a, b, None])
    assert both.n_cells == a.n_cells + b.n_cells
    assert both.cell_data["rgb"][-1].tolist() == [7, 7, 7]
    assert "Normals" not in both.point_data
    assert "Normals" in _merge_models([a, a]).point_data
    assert _merge_models([]) is None


# --- The window picks the parts ----------------------------------------------------------

def _window(no_3d, monkeypatch, nets=("VOUT",), found=None, cli=("no-such-kicad-cli",)):
    from kipi.sources.kicad_ipc import models3d
    from kipi.sources.kicad_ipc.source import InstalledKiCad
    from kipi.gui.main_window import MainWindow
    w = MainWindow(tool=InstalledKiCad())
    w.snapshot = board()
    w.solve_panel.setup = two_phase()
    monkeypatch.setattr(w, "_checked_set", lambda: frozenset(nets))
    shown, asked, said = [], [], []
    w.viewer.set_models = lambda models, top, kicad_top=None: shown.append((models, top))
    monkeypatch.setattr(w, "log", lambda text, level=0: said.append(text))
    monkeypatch.setattr(models3d, "find_kicad_cli", lambda kicad=None: list(cli) if cli else None)
    monkeypatch.setattr(models3d, "board_file", lambda snap: "b.kicad_pcb")

    def load(cli, board, refs, progress=None):
        asked.append(list(refs))
        return {r: (found or {}).get(r) for r in refs}

    monkeypatch.setattr(models3d, "load_models", load)
    return w, shown, asked, said


def _settle(app, w):
    if w._model_task is not None:
        w._model_task.wait(10000)
    for _ in range(5):
        app.processEvents()


def test_every_part_on_the_board_gets_its_model(app, no_3d, monkeypatch):
    """Not only the setup's parts, nor only those on the nets shown (#96)."""
    mesh = _part()
    w, shown, asked, _ = _window(no_3d, monkeypatch, found={"L1": mesh, "U2": mesh})
    w._sync_models()
    _settle(app, w)
    parts = sorted({p.footprint for p in w.snapshot.pads} - {""})
    assert asked == [parts] and {"J1", "Q1", "U2"} <= set(parts)
    models, top = shown[-1]
    assert set(models) == {"L1", "U2"}                    # the others have no model
    assert models["L1"] == (mesh, "top")
    assert top == pytest.approx(w.snapshot.stackup.board_thickness_mm)

    # Known parts are not asked for again, whatever nets are shown.
    monkeypatch.setattr(w, "_checked_set", lambda: frozenset({"VOUT", "VIN"}))
    w._sync_models()
    _settle(app, w)
    assert asked[1:] == []

    w.chk_models.setChecked(False)
    w._sync_models()
    assert shown[-1][0] == {}
    w.close()


def test_without_kicad_cli_no_models_are_loaded(app, no_3d, monkeypatch):
    w, shown, asked, said = _window(no_3d, monkeypatch, cli=None)
    w._sync_models()
    w._sync_models()
    assert w._model_task is None and asked == []
    assert sum("kicad-cli" in s for s in said) == 1        # said once
    assert shown[-1][0] == {}
    w.close()


def test_a_load_for_an_earlier_board_is_dropped(app, no_3d, monkeypatch):
    w, shown, asked, _ = _window(no_3d, monkeypatch, found={"L1": _part()})
    w._sync_models()
    w._model_gen += 1                                     # a new board was loaded meanwhile
    _settle(app, w)
    assert "L1" not in w._model_cache
    w.close()


def test_a_closed_window_loads_no_more_models(app, no_3d, monkeypatch):
    w, shown, asked, _ = _window(no_3d, monkeypatch)
    w._model_timer.start()
    w.close()
    assert not w._model_timer.isActive()
    w._sync_models()
    assert asked == [] and w._model_task is None


def test_models_are_guessed_only_when_asked(app, no_3d, monkeypatch):
    """With no board file, no models are guessed until Guess models is clicked; then only
    the parts with none are guessed (#130)."""
    from kipi.sources.kicad_ipc import model_guess, models3d
    mesh = _part()
    guessed = []

    class Guess:
        label = "a guess"

        def load(self, refs, progress=None):
            guessed.append(list(refs))
            return {r: mesh if r == "L1" else None for r in refs}

    w, shown, asked, said = _window(no_3d, monkeypatch)
    monkeypatch.setattr(models3d, "board_file", lambda snap: None)
    monkeypatch.setattr(model_guess, "guessed_models", lambda cli, snap: Guess())
    w._sync_models()
    _settle(app, w)
    assert guessed == [] and asked == [] and shown[-1][0] == {}
    assert any("Guess models" in s for s in said)          # the log says how

    w.chk_models.setChecked(False)
    w.guess_models()
    assert w.chk_models.isChecked()                        # ticked for the models
    w._sync_models()
    _settle(app, w)
    parts = sorted({p.footprint for p in w.snapshot.pads} - {""})
    assert guessed == [parts]
    assert set(shown[-1][0]) == {"L1"}

    w.guess_models()                                       # again: only those still without
    w._sync_models()
    _settle(app, w)
    assert guessed[-1] == [r for r in parts if r != "L1"]
    w._sync_models()
    _settle(app, w)
    assert len(guessed) == 2                               # and not again unasked
    w.close()


def test_a_guess_with_no_library_says_why(app, no_3d, monkeypatch):
    from kipi.sources.kicad_ipc import model_guess, models3d
    w, shown, asked, said = _window(no_3d, monkeypatch)
    monkeypatch.setattr(models3d, "board_file", lambda snap: None)
    monkeypatch.setattr(model_guess, "library_dirs", lambda cli: [])
    w.guess_models()
    w._sync_models()
    assert w._model_task is None
    assert any("library was not found" in s for s in said)
    w.close()
