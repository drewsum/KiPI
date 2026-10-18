"""Embedded 3D viewport (pyvistaqt QtInteractor) for board copper and, later, results."""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pyvista as pv
from pyvistaqt import QtInteractor
from vtkmodules.util.numpy_support import numpy_to_vtk
from vtkmodules.vtkCommonCore import vtkPoints, vtkStringArray
from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
from vtkmodules.vtkRenderingCore import vtkActor, vtkActor2D, vtkCellPicker, vtkPolyDataMapper
from vtkmodules.vtkRenderingLabel import vtkLabeledDataMapper
from qtpy.QtCore import QTimer, Signal
from qtpy.QtWidgets import QVBoxLayout, QWidget

from kipi.core.viz.batch import Chunk, MeshBatcher
from kipi.dc.viz.results import FIELDS, ResultScene, flat_arrows, scale_decimals
from kipi.core.viz.meshes import NetScene

_NET_COLORS = [(0.90, 0.35, 0.25), (0.25, 0.60, 0.90), (0.95, 0.75, 0.20), (0.40, 0.80, 0.45),
               (0.80, 0.45, 0.85), (0.30, 0.80, 0.80), (0.95, 0.55, 0.30), (0.65, 0.65, 0.65)]
_VIA_COLOR = (0.85, 0.85, 0.80)
_PAD_COLOR = (1.0, 0.85, 0.35)
_BOARD_COLOR = (0.15, 0.40, 0.20)


# Surface properties per mesh category: (opacity, specular)
_CATEGORY_LOOK = {"cu": (1.0, 0.4), "pad": (0.9, 0.0), "via": (1.0, 0.6)}
# Copper drawn under the via-current view, for context
_VIA_VIEW_COPPER_OPACITY = 0.35
DEPTH_PEELS = 4          # see-through layers sorted per pixel (pyvista's default)
_MODEL_SPECULAR = 0.3
# PI Models markers (#84), by kind: (dot color, label color). Picked to stand out from
# KiCad's red top and blue bottom copper and its yellow pads.
_PI_COLORS = {"vrm": ((0.25, 1.0, 0.35), (0.10, 0.45, 0.16)),
              "load": ((0.10, 0.95, 1.0), (0.04, 0.38, 0.45)),
              "link": ((1.0, 0.35, 1.0), (0.48, 0.14, 0.48)),
              "cap": ((0.95, 0.95, 0.95), (0.32, 0.33, 0.37)),
              "probe": ((0.70, 0.55, 1.0), (0.33, 0.20, 0.62))}
_PI_POINT_PX = {"power": 14, "ground": 9}       # rail pins, ground pins
_PI_LABEL_LIFT_MM = 1.0
# The part selected in the setup (#96): larger dots and its name, in amber.
_PI_SELECTED_COLORS = ((1.0, 0.8, 0.1), (0.75, 0.42, 0.0))
_PI_SELECTED_PX = 22
# Newly selected, it blinks between amber and red for a moment, to be found at a glance.
_PI_BLINK_COLORS = ((1.0, 0.12, 0.12), (0.70, 0.0, 0.0))
_PI_BLINK_MS = 250
_PI_BLINKS = 6
# A click this near a marker's name (half its height; the width follows the name) or one
# of its dots, in pixels, picks the marker.
_PI_LABEL_HALF_H_PX = 9
_PI_DOT_PICK_PX = 8


def _bar_format(lo: float, hi: float, log_scale: bool) -> str:
    """Tick label format with enough decimals to tell the labels apart.

    A 3.3 V rail that sags by 0.4 mV needs five decimals, or every tick reads "3.3".
    """
    if log_scale:
        return "%.2g"
    span = abs(hi - lo)
    if span <= 0:
        return "%.4g"
    step_decimals = max(0, int(np.ceil(-np.log10(span / 5))) + 1)
    if max(abs(lo), abs(hi)) >= 1e4 or step_decimals > 9:
        return "%.4g"
    return f"%.{scale_decimals(lo, hi)}f"


# The scale bar sits against the view's right edge (next to the setup panes), leaving a
# small gap, so it covers as little of the board as it can.
_BAR_RIGHT = 0.995
_BAR_WIDTH = 0.03
_BAR_Y, _BAR_HEIGHT = 0.25, 0.5


def _bar_args(title: str, fmt: str) -> dict:
    return dict(title=title, vertical=True, position_x=_BAR_RIGHT - _BAR_WIDTH,
                position_y=_BAR_Y, height=_BAR_HEIGHT, width=_BAR_WIDTH, fmt=fmt,
                title_font_size=14, color="white", label_font_size=12, n_labels=6)


def _bar_title_actor(title: str):
    """The bar's title, drawn apart from the bar and ending at its right edge: VTK centers
    a bar's own title over the bar, which pushes a long one off the edge of the view."""
    from vtkmodules.vtkRenderingCore import vtkTextActor
    actor = vtkTextActor()
    actor.SetInput(title)
    prop = actor.GetTextProperty()
    prop.SetFontSize(14)
    prop.SetColor(1.0, 1.0, 1.0)
    prop.SetJustificationToRight()
    prop.SetVerticalJustificationToBottom()
    actor.GetPositionCoordinate().SetCoordinateSystemToNormalizedViewport()
    # actor.SetPosition(_BAR_RIGHT, _BAR_Y + _BAR_HEIGHT - 0.012)   # just over the bar
    actor.SetPosition(_BAR_RIGHT, _BAR_Y + _BAR_HEIGHT)   # just over the bar
    actor.SetPickable(False)
    return actor


def apply_limits(lo: float, hi: float, log_scale: bool,
                 limits: Optional[Tuple[float, float]]) -> Tuple[float, float]:
    """The color range drawn: ``limits`` (min, max) typed by the user in place of the
    automatic ``lo``..``hi``. A log scale cannot start at or below zero, so a minimum
    there falls back to four decades below the maximum."""
    if limits is not None:
        lo, hi = (float(x) for x in limits)
    if hi <= lo:
        hi = lo + 1e-12
    if log_scale and lo <= 0:
        lo = max(hi * 1e-4, 1e-30)
    return lo, hi


def _copper_clim(scene: ResultScene, field: str) -> Tuple[float, float, bool]:
    """Automatic color limits ``(lo, hi, log_scale)`` for a copper field over every
    layer, whichever are shown (the user sets the limits to look closer)."""
    arr, assoc, _, _ = FIELDS[field]
    data = [(lr.mesh.point_data if assoc == "point" else lr.mesh.cell_data)[arr]
            for lr in scene.layers.values()]
    vals = np.concatenate([d[np.isfinite(d)] for d in data]) if data else np.zeros(0)
    if not len(vals):
        lo, hi = scene.ranges[field]
    else:
        lo, hi = float(vals.min()), float(vals.max())
        if field in ("current_density", "power_density"):
            # Hot spots at pad corners dwarf everything else; clip the scale at the
            # 99.5th percentile (the probe still reports true values).
            hi = float(np.percentile(vals, 99.5))
    if hi <= lo:
        hi = lo + 1e-12
    log_scale = field == "power_density"
    if log_scale:
        lo = max(hi * 1e-4, 1e-30)      # four decades: loss is very concentrated
    return lo, hi, log_scale


def _chunk_actor(category, layer_opacity: float = 1.0) -> vtkActor:
    """A bare VTK actor colored by the chunk's per-cell ``rgb`` array.

    Chunk actors bypass ``Plotter.add_mesh``: its name bookkeeping walks every actor in
    the renderer on each call, and pyvista's mapper wrapper builds color lookup tables
    that direct-RGB meshes never use.
    """
    mapper = vtkPolyDataMapper()
    mapper.SetScalarModeToUseCellFieldData()
    mapper.SelectColorArray("rgb")
    mapper.SetColorModeToDirectScalars()
    mapper.ScalarVisibilityOn()
    actor = vtkActor()
    actor.SetMapper(mapper)
    opacity, specular = _CATEGORY_LOOK[category[0]]
    prop = actor.GetProperty()
    prop.SetOpacity(opacity * (layer_opacity if category[0] in ("cu", "pad") else 1.0))
    prop.SetSpecular(specular)
    prop.SetSpecularPower(20.0)
    prop.SetInterpolationToFlat()
    return actor


def _merge_models(meshes: List[pv.PolyData]) -> Optional[pv.PolyData]:
    """One mesh of many part models, keeping each triangle's ``rgb`` and, when every
    model has them, the point normals that smooth its curved faces."""
    meshes = [m for m in meshes if m is not None and m.n_cells]
    if not meshes:
        return None
    offsets = np.cumsum([0] + [m.n_points for m in meshes[:-1]])
    faces = np.concatenate([m.regular_faces + o for m, o in zip(meshes, offsets)])
    cells = np.empty((len(faces), 4), dtype=np.int64)
    cells[:, 0] = 3
    cells[:, 1:] = faces
    out = pv.PolyData(np.concatenate([m.points for m in meshes]), cells.ravel())
    out.cell_data["rgb"] = np.concatenate([np.asarray(m.cell_data["rgb"]) for m in meshes])
    if all("Normals" in m.point_data for m in meshes):
        out.point_data["Normals"] = np.concatenate([np.asarray(m.point_data["Normals"])
                                                    for m in meshes])
        out.point_data.active_normals_name = "Normals"
    return out


def _model_actor(mesh: pv.PolyData) -> vtkActor:
    """A bare actor for part models, colored by their per-triangle ``rgb``. Models are
    never picked: a click is meant for the copper beneath them."""
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(mesh)
    mapper.SetScalarModeToUseCellFieldData()
    mapper.SelectColorArray("rgb")
    mapper.SetColorModeToDirectScalars()
    mapper.ScalarVisibilityOn()
    actor = vtkActor()
    actor.SetMapper(mapper)
    actor.PickableOff()
    prop = actor.GetProperty()
    prop.SetSpecular(_MODEL_SPECULAR)
    prop.SetSpecularPower(20.0)
    prop.SetInterpolationToGouraud()
    return actor


def _vtk_points(pts: np.ndarray) -> vtkPolyData:
    """Bare polydata of one vertex per point."""
    points = vtkPoints()
    points.SetData(numpy_to_vtk(np.ascontiguousarray(pts, dtype=float), deep=True))
    verts = vtkCellArray()
    for i in range(len(pts)):
        verts.InsertNextCell(1)
        verts.InsertCellPoint(i)
    poly = vtkPolyData()
    poly.SetPoints(points)
    poly.SetVerts(verts)
    return poly


def _pi_dots_actor(pts: np.ndarray, color, size: float) -> vtkActor:
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(_vtk_points(pts))
    actor = vtkActor()
    actor.SetMapper(mapper)
    actor.PickableOff()
    prop = actor.GetProperty()
    prop.SetColor(*color)
    prop.SetPointSize(size)
    prop.SetRenderPointsAsSpheres(True)
    return actor


def _pi_labels_actor(pts: np.ndarray, names: List[str], color) -> vtkActor2D:
    poly = _vtk_points(pts)
    arr = vtkStringArray()
    arr.SetName("names")
    for n in names:
        arr.InsertNextValue(n)
    poly.GetPointData().AddArray(arr)
    mapper = vtkLabeledDataMapper()
    mapper.SetInputData(poly)
    mapper.SetLabelModeToLabelFieldData()
    mapper.SetFieldDataName("names")
    text = mapper.GetLabelTextProperty()
    text.SetFontSize(12)
    text.BoldOn()
    text.ItalicOff()
    text.ShadowOff()
    text.SetColor(1.0, 1.0, 1.0)
    text.SetBackgroundColor(*color)
    text.SetBackgroundOpacity(0.75)
    text.SetJustificationToCentered()
    actor = vtkActor2D()
    actor.SetMapper(mapper)
    actor.PickableOff()
    return actor


class Viewer3D(QWidget):
    """3D view of the board body plus individually added nets, with visibility controls.

    Nets are packed into a few merged meshes by :class:`MeshBatcher`; edits mark chunks
    dirty and :meth:`render` rebuilds only those before drawing.
    """

    #: Emitted with True when results replace the copper view and False when they go.
    result_shown = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plotter = QtInteractor(self)
        layout.addWidget(self.plotter.interactor)
        self.plotter.set_background("#1e1f24", top="#3a3d46")
        # FXAA is a cheap post-process; SSAA re-renders the scene several times per frame.
        self.plotter.enable_anti_aliasing("fxaa")
        self.plotter.add_axes()

        self._batch = MeshBatcher()
        self._chunk_actors: Dict[Chunk, vtkActor] = {}
        self._net_color: Dict[str, int] = {}                  # net -> color index
        self._layer_color: Dict[Tuple[str, int], tuple] = {}  # (net, layer) -> layer color
        self._next_color = 0
        self._board_actors: List[object] = []
        self._hidden_layers: set = set()
        self._show_pads = True
        self._show_vias = True
        self._show_board = True
        self._show_models = True            # in the copper (geometry) view
        self._show_models_result = False    # over results: off until asked for
        self._show_pi = False               # PI Models markers, copper view only
        self._pi_markers: list = []
        self._pi_actors: Dict[str, object] = {}      # name -> actor
        self._pi_selected: Optional[Tuple[str, str]] = None   # (kind, item) in the setup
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(_PI_BLINK_MS)
        self._blink_timer.timeout.connect(self._blink_step)
        self._blink_left = 0                # color changes still to show
        self._models: Dict[str, Tuple[pv.PolyData, str]] = {}   # ref -> (mesh, side)
        self._model_actors: Dict[str, vtkActor] = {}             # side -> actor
        self._board_top = 0.0
        self._model_board = 0.0   # board thickness the part models were placed for
        self._color_mode = "layer"
        self._zscale = 1.0
        self._layer_opacity = 1.0
        self._depth_peeling = False
        self._pick_cb: Optional[Callable[[Tuple[float, float, float]], None]] = None
        self._result: Optional[ResultScene] = None
        self._result_field = "voltage"
        self._result_actors: Dict[int, object] = {}      # layer id -> actor
        self._result_via_actor = None
        self._scalar_bars: List[str] = []
        self._bar_titles: list = []
        self._color_limits: Optional[Tuple[float, float]] = None   # user's, or automatic
        self._clim: Optional[Tuple[float, float]] = None           # the range drawn
        self._arrows_on = False
        self._arrow_actors: Dict[int, object] = {}          # layer id -> actor
        # Clicks are handled here rather than with pyvista's point picking, so a click can
        # also select a via by its on-screen distance (vias are only a few pixels wide).
        self._picker = vtkCellPicker()
        self._picker.SetTolerance(0.0005)
        self._press_pos = None
        iren = self.plotter.iren
        iren.add_observer("LeftButtonPressEvent", self._on_left_press)
        iren.add_observer("LeftButtonReleaseEvent", self._on_left_release)

    # ------------------------------------------------------------------ scene
    def set_board(self, body: Optional[pv.PolyData], outline: Optional[pv.PolyData],
                  reset_view: bool = True):
        """Start a new board: drop every actor, draw the board body and frame the camera
        (unless ``reset_view`` is False, as when only the stackup changed)."""
        p = self.plotter
        renderer = p.renderer
        for a in self._chunk_actors.values():
            renderer.RemoveActor(a)
        self._chunk_actors.clear()
        self._remove_model_actors()
        self._models = {}
        self._batch = MeshBatcher()
        self._net_color.clear()
        self._layer_color.clear()
        self._clear_result_actors()
        self._set_result(None)
        p.clear_actors()
        self._remove_pi_actors()
        self._pi_markers = []
        self._board_actors.clear()
        self._hidden_layers.clear()
        self._next_color = 0
        if body is not None:
            self._board_actors.append(p.add_mesh(body, color=_BOARD_COLOR, opacity=0.18,
                                                 name="board", pickable=False,
                                                 reset_camera=False, render=False))
        if outline is not None:
            self._board_actors.append(p.add_mesh(outline, color="#9fd39f", line_width=1.5,
                                                 name="outline", pickable=False,
                                                 reset_camera=False, render=False))
        for a in self._board_actors:
            a.SetScale(1.0, 1.0, self._zscale)
        self._apply_visibility()
        if reset_view:
            self.view_iso()

    def nets(self) -> List[str]:
        return list(self._net_color)

    def add_net(self, ns: NetScene, render: bool = True):
        """Add (or replace) one net's meshes without touching the others.

        Pass ``render=False`` when adding several nets and call :meth:`render` once.
        """
        if ns.net in self._net_color:
            self._batch.remove(ns.net)
        else:
            self._net_color[ns.net] = self._next_color
            self._next_color += 1
        for lm in ns.copper:
            self._layer_color[(ns.net, lm.layer_id)] = lm.color
            self._batch.add(ns.net, ("cu", lm.layer_id), lm.mesh,
                            self._copper_color(ns.net, lm.color))
        for lid, m in ns.pads.items():
            self._batch.add(ns.net, ("pad", lid), m, _PAD_COLOR)
        if ns.vias is not None:
            self._batch.add(ns.net, ("via", -1), ns.vias, _VIA_COLOR)
        if render:
            self.render()

    def remove_net(self, net: str, render: bool = True):
        if net not in self._net_color:
            return
        self._batch.remove(net)
        del self._net_color[net]
        for key in [k for k in self._layer_color if k[0] == net]:
            del self._layer_color[key]
        if render:
            self.render()

    # ------------------------------------------------------------ part models
    def models(self) -> List[str]:
        return list(self._models)

    def set_models(self, models: Dict[str, Tuple[pv.PolyData, str]], board_top_mm: float,
                   model_board_mm: Optional[float] = None):
        """Draw these part models (ref -> (mesh, "top" or "bottom")) in place of any shown.

        Meshes are in the view's frame at true height (see
        :func:`kipi.sources.kicad_ipc.models3d.to_mesh`), for a board ``model_board_mm`` thick
        (KiCad's; default ``board_top_mm``). The Z scale stretches the board but not the
        parts: the top side's parts are lifted to sit on the stretched board, and on a
        board made thicker or thinner in the Stackup pane.
        """
        model_board = float(board_top_mm if model_board_mm is None else model_board_mm)
        if (models.keys() == self._models.keys() and board_top_mm == self._board_top
                and model_board == self._model_board
                and all(models[r][0] is self._models[r][0] for r in models)):
            return
        self._models = dict(models)
        self._board_top = float(board_top_mm)
        self._model_board = model_board
        self._remove_model_actors()
        renderer = self.plotter.renderer
        for side in ("top", "bottom"):
            mesh = _merge_models([m for m, s in self._models.values() if s == side])
            if mesh is None:
                continue
            actor = _model_actor(mesh)
            renderer.AddActor(actor)
            self._model_actors[side] = actor
        self._place_models()
        self._apply_visibility()

    def _remove_model_actors(self):
        renderer = self.plotter.renderer
        for actor in self._model_actors.values():
            renderer.RemoveActor(actor)
        self._model_actors.clear()

    def _place_models(self):
        top = self._model_actors.get("top")
        if top is not None:
            top.SetPosition(0.0, 0.0, self._board_top * self._zscale - self._model_board)

    # ------------------------------------------------------- PI Models markers
    def set_pi_markers(self, markers):
        """Mark the setup's parts: a point on each of their pads and the part's name. DC
        mode's are its VRMs, loads, series links and capacitors
        (:func:`kipi.dc.viz.markers.pi_markers`), AC mode's its VRMs, series links, probes
        and capacitors (:func:`kipi.ac.viz.markers.ac_markers`). Shown over the copper
        only, not over results."""
        self._pi_markers = list(markers)
        self._draw_pi_markers()
        self._apply_visibility()

    def _remove_pi_actors(self):
        renderer = self.plotter.renderer
        for actor in self._pi_actors.values():
            renderer.RemoveViewProp(actor)
        self._pi_actors = {}

    def _draw_pi_markers(self):
        """Bare VTK actors, like the copper's: pyvista's ``add_points`` and
        ``add_point_labels`` here, while nets were built in the background, hung the
        window in its next render."""
        self._remove_pi_actors()
        renderer = self.plotter.renderer

        def view(pts):
            return np.column_stack([pts[:, 0], -pts[:, 1], pts[:, 2] * self._zscale])

        for kind, colors in _PI_COLORS.items():
            parts = [m for m in self._pi_markers if m.kind == kind]
            for which, size in _PI_POINT_PX.items():
                pts = [getattr(m, which) for m in parts if len(getattr(m, which))]
                if not pts:
                    continue
                self._pi_actors[f"pi_{kind}_{which}"] = _pi_dots_actor(
                    view(np.vstack(pts)), colors[0], size)
            if parts:
                name = f"pi_{kind}_labels"
                anchors = view(np.array([m.anchor for m in parts]))
                anchors[:, 2] += _PI_LABEL_LIFT_MM        # clear of the part's dots
                self._pi_actors[name] = _pi_labels_actor(anchors, [m.name for m in parts],
                                                         colors[1])
        sel = self._selected_marker()
        if sel is not None:             # drawn last, so over the part's own dots and name
            self._pi_actors["pi_selected_dots"] = _pi_dots_actor(
                view(np.vstack([sel.power, sel.ground])), _PI_SELECTED_COLORS[0],
                _PI_SELECTED_PX)
            anchor = view(sel.anchor[None, :])
            anchor[:, 2] += _PI_LABEL_LIFT_MM
            self._pi_actors["pi_selected_label"] = _pi_labels_actor(
                anchor, [sel.name], _PI_SELECTED_COLORS[1])
        for actor in self._pi_actors.values():
            renderer.AddViewProp(actor)

    @property
    def pi_marker_names(self) -> List[str]:
        return [m.name for m in self._pi_markers]

    @property
    def pi_markers(self) -> list:
        return list(self._pi_markers)

    def _selected_marker(self):
        return next((m for m in self._pi_markers if (m.kind, m.item) == self._pi_selected),
                    None)

    def set_pi_selected(self, key: Optional[Tuple[str, str]]):
        """Highlight the part selected in the setup, ``(kind, item name)``, or none (#96).
        Shown with the PI Models markers."""
        if key == self._pi_selected:
            return
        self._pi_selected = key
        self._draw_pi_markers()
        self._apply_visibility()
        self._start_blink()

    def _start_blink(self):
        """Blink the selected part's dots and name between amber and red a few times."""
        self._blink_timer.stop()
        self._blink_left = 2 * _PI_BLINKS if self._selected_marker() is not None else 0
        if self._blink_left:
            self._blink_timer.start()

    def _blink_step(self):
        self._blink_left -= 1
        if self._blink_left <= 0:
            self._blink_timer.stop()
        self._paint_selected(_PI_BLINK_COLORS if self._blink_left % 2 else _PI_SELECTED_COLORS)
        if self._show_pi and self._result is None:
            self.plotter.render()

    def _paint_selected(self, colors):
        """Color the selected part's dots and name: ``(dot color, label color)``."""
        dots = self._pi_actors.get("pi_selected_dots")
        if dots is not None:
            dots.GetProperty().SetColor(*colors[0])
        label = self._pi_actors.get("pi_selected_label")
        if label is not None:
            label.GetMapper().GetLabelTextProperty().SetBackgroundColor(*colors[1])

    def pi_marker_at(self, screen_xy):
        """The PI Models marker whose name, or else one of whose dots, is at ``screen_xy``
        (display pixels), or None. Only while the markers are shown (#96)."""
        if not self._show_pi or self._result is not None or not self._pi_markers:
            return None
        p = np.asarray(screen_xy, dtype=float)
        lift = _PI_LABEL_LIFT_MM / self._zscale
        anchors = np.array([[*m.anchor[:2], m.anchor[2] + lift] for m in self._pi_markers])
        xy, behind = self._to_screen(anchors)
        hits = [(abs(p[1] - a[1]), m) for m, a, hidden in zip(self._pi_markers, xy, behind)
                if not hidden and abs(p[1] - a[1]) <= _PI_LABEL_HALF_H_PX
                and abs(p[0] - a[0]) <= 6 + 4 * len(m.name)]
        if hits:
            return min(hits, key=lambda h: h[0])[1]
        best, best_d = None, float(_PI_DOT_PICK_PX)
        for m in self._pi_markers:
            xy, behind = self._to_screen(np.vstack([m.power, m.ground]))
            d = np.linalg.norm(xy - p, axis=1)[~behind]
            if len(d) and d.min() <= best_d:
                best, best_d = m, float(d.min())
        return best

    def focus_on(self, pts: np.ndarray, min_span_mm: float = 6.0):
        """Center the view on ``pts`` (N, 3), KiCad coords, near enough to see them with
        some board around them, looking from the same direction (#96)."""
        pts = np.asarray(pts, dtype=float).reshape(-1, 3)
        if not len(pts):
            return
        c = pts.mean(axis=0)
        center = np.array([c[0], -c[1], c[2] * self._zscale])
        span = 2.5 * max(float(np.ptp(pts[:, :2], axis=0).max()), min_span_mm)
        cam = self.plotter.renderer.GetActiveCamera()
        pos, focal = np.array(cam.GetPosition()), np.array(cam.GetFocalPoint())
        dist = float(np.linalg.norm(pos - focal)) or 1.0
        direction = (pos - focal) / dist
        if cam.GetParallelProjection():
            cam.SetParallelScale(span / 2)
        else:
            dist = span / 2 / np.tan(np.radians(cam.GetViewAngle()) / 2)
        cam.SetFocalPoint(*center)
        cam.SetPosition(*(center + direction * dist))
        self.plotter.renderer.ResetCameraClippingRange()
        self.plotter.render()

    def _flush(self):
        """Rebuild changed chunks and sync their actors."""
        renderer = self.plotter.renderer
        for chunk in self._batch.take_dirty():
            actor = self._chunk_actors.get(chunk)
            mesh = chunk.build()
            if mesh is None:
                if actor is not None:
                    renderer.RemoveActor(actor)
                    del self._chunk_actors[chunk]
                continue
            if actor is None:
                actor = _chunk_actor(chunk.category, self._layer_opacity)
                actor.SetScale(1.0, 1.0, self._zscale)
                renderer.AddActor(actor)
                self._chunk_actors[chunk] = actor
                self._apply_actor_visibility(chunk.category, actor)
            actor.GetMapper().SetInputData(mesh)

    def render(self):
        self._flush()
        self.plotter.render()

    def clear(self):
        self.set_board(None, None)

    # ------------------------------------------------------------ appearance
    def _copper_color(self, net: str, layer_color):
        if self._color_mode == "net":
            return _NET_COLORS[self._net_color[net] % len(_NET_COLORS)]
        return layer_color

    def set_color_mode(self, mode: str):
        self._color_mode = mode
        for (net, lid), layer_color in self._layer_color.items():
            self._batch.set_color(net, ("cu", lid), self._copper_color(net, layer_color))
        self.render()

    def set_layer_visible(self, layer_id: int, visible: bool):
        (self._hidden_layers.discard if visible else self._hidden_layers.add)(layer_id)
        self._apply_visibility()

    def set_feature_visible(self, feature: str, visible: bool):
        if feature == "models" and self._result is not None:
            feature = "models_result"       # the result view keeps its own setting
        setattr(self, f"_show_{feature}", visible)
        self._apply_visibility()

    @property
    def models_shown(self) -> bool:
        """Whether part models are shown in the current view (copper or results)."""
        return self._show_models_result if self._result is not None else self._show_models

    @property
    def models_wanted(self) -> bool:
        """Whether either view shows part models, so they are worth loading."""
        return self._show_models or self._show_models_result

    def _apply_actor_visibility(self, category, actor):
        kind, lid = category
        if kind == "cu":
            visible = lid not in self._hidden_layers
        elif kind == "pad":
            visible = self._show_pads and lid not in self._hidden_layers
        else:
            visible = self._show_vias
        actor.SetVisibility(visible)

    def _apply_visibility(self):
        showing_result = self._result is not None
        for chunk, actor in self._chunk_actors.items():
            if showing_result:
                actor.SetVisibility(False)
            else:
                self._apply_actor_visibility(chunk.category, actor)
        for lid, actor in self._result_actors.items():
            actor.SetVisibility(lid not in self._hidden_layers)
        for lid, actor in self._arrow_actors.items():
            actor.SetVisibility(lid not in self._hidden_layers)
        if self._result_via_actor is not None:
            # In the via-current view the vias are the result, so always show them.
            self._result_via_actor.SetVisibility(self._show_vias
                                                 or self._result_field == "via_current")
        for a in self._board_actors:
            a.SetVisibility(self._show_board)
        models = self._show_models_result if showing_result else self._show_models
        for a in self._model_actors.values():
            a.SetVisibility(models)
        for a in self._pi_actors.values():
            a.SetVisibility(self._show_pi and not showing_result)
        self.plotter.render()

    def set_zscale(self, s: float):
        self._zscale = max(1.0, float(s))
        for a in self._board_actors:
            a.SetScale(1.0, 1.0, self._zscale)
        for a in self._chunk_actors.values():
            a.SetScale(1.0, 1.0, self._zscale)
        for a in self._result_actors.values():
            a.SetScale(1.0, 1.0, self._zscale)
        if self._result_via_actor is not None:
            self._result_via_actor.SetScale(1.0, 1.0, self._zscale)
        for a in self._arrow_actors.values():
            a.SetScale(1.0, 1.0, self._zscale)
        self._place_models()
        if self._pi_markers:
            self._draw_pi_markers()
            self._apply_visibility()
        self.plotter.reset_camera_clipping_range()
        self.plotter.render()

    @property
    def zscale(self) -> float:
        return self._zscale

    def set_layer_opacity(self, opacity: float):
        """Draw the copper layers (plain and result) and their pads at ``opacity`` (0..1)
        of their usual opacity, to see the layers beneath. Vias and arrows stay solid."""
        self._layer_opacity = min(1.0, max(0.0, float(opacity)))
        for chunk, actor in self._chunk_actors.items():
            kind = chunk.category[0]
            if kind in ("cu", "pad"):
                actor.GetProperty().SetOpacity(_CATEGORY_LOOK[kind][0] * self._layer_opacity)
        for actor in self._result_actors.values():
            self._apply_result_opacity(actor)
        self._set_depth_peeling(self._layer_opacity < 1.0)
        self.plotter.render()

    def _apply_result_opacity(self, actor):
        base = _VIA_VIEW_COPPER_OPACITY if self._result_field == "via_current" else 1.0
        actor.GetProperty().SetOpacity(base * self._layer_opacity)

    def _set_depth_peeling(self, on: bool):
        """Sort see-through layers properly while any are see-through. Some graphics
        drivers lack depth peeling; VTK then blends unsorted, which only looks a little
        off, so a failure here is ignored.

        The renderers are set directly rather than through pyvista's
        ``enable_depth_peeling``: that first renders a probe scene in a hidden window of
        its own, which crashes the process (an access violation, not an exception) on
        some drivers, e.g. Windows without a GPU. VTK checks support itself when it
        renders and falls back to unsorted blending."""
        if on == self._depth_peeling:
            return
        try:
            for renderer in self.plotter.renderers:
                renderer.SetUseDepthPeeling(on)
                if on:
                    renderer.SetMaximumNumberOfPeels(DEPTH_PEELS)
                    renderer.SetOcclusionRatio(0.0)
            self._depth_peeling = on
        except Exception:
            pass

    @property
    def layer_opacity(self) -> float:
        return self._layer_opacity

    # ---------------------------------------------------------------- results
    @property
    def result(self) -> Optional[ResultScene]:
        return self._result

    def show_result(self, scene: ResultScene, field: Optional[str] = None,
                    limits: Optional[Tuple[float, float]] = None):
        """Replace the copper view with solver results colored by ``field``, over the color
        range ``limits`` (min, max), or the field's whole range when None."""
        self._clear_result_actors()
        self._set_result(scene)
        if field is not None:
            self._result_field = field
        self._color_limits = limits
        self._draw_result()

    def set_result_field(self, field: str, limits: Optional[Tuple[float, float]] = None):
        self._result_field = field
        self._color_limits = limits
        if self._result is not None:
            self._clear_result_actors()
            self._draw_result()

    def set_color_limits(self, limits: Optional[Tuple[float, float]]):
        """Color the result over ``limits`` (min, max), or its whole range when None."""
        self._color_limits = limits
        if self._result is not None:
            self._clear_result_actors()
            self._draw_result()

    def color_range(self) -> Optional[Tuple[float, float]]:
        """The color range the result is drawn over, or None with no result shown."""
        return self._clim if self._result is not None else None

    def clear_result(self):
        self._clear_result_actors()
        self._set_result(None)
        self._apply_visibility()

    def _set_result(self, scene: Optional[ResultScene]):
        was = self._result is not None
        self._result = scene
        if was != (scene is not None):
            self.result_shown.emit(scene is not None)

    def _clear_result_actors(self):
        p = self.plotter
        for a in self._result_actors.values():
            p.remove_actor(a, render=False)
        if self._result_via_actor is not None:
            p.remove_actor(self._result_via_actor, render=False)
        for a in self._arrow_actors.values():
            p.remove_actor(a, render=False)
        self._arrow_actors.clear()
        for title in self._scalar_bars:
            try:
                p.remove_scalar_bar(title, render=False)
            except (KeyError, ValueError, StopIteration):
                pass
        for actor in self._bar_titles:
            p.renderer.RemoveViewProp(actor)
        self._result_actors.clear()
        self._result_via_actor = None
        self._scalar_bars.clear()
        self._bar_titles.clear()

    def _draw_result(self):
        scene = self._result
        arr, assoc, label, unit = FIELDS[self._result_field]
        if assoc == "via":
            self._draw_via_result(scene, arr, f"{label} ({unit})")
        else:
            self._draw_copper_result(scene, arr, assoc, f"{label} ({unit})")
        if self._arrows_shown():
            self._draw_arrows(scene)
        self._apply_visibility()

    # ------------------------------------------------------------------ arrows
    ARROW_FIELD = "current_density"

    def _arrows_shown(self) -> bool:
        """Arrows belong to the current-density view only."""
        return self._arrows_on and self._result_field == self.ARROW_FIELD

    def set_arrows_visible(self, on: bool):
        self._arrows_on = bool(on)
        if self._result is None:
            return
        for a in self._arrow_actors.values():
            self.plotter.remove_actor(a, render=False)
        self._arrow_actors.clear()
        if self._arrows_shown():
            self._draw_arrows(self._result)
        self._apply_visibility()

    def _draw_arrows(self, scene):
        """Flat white arrows along the current, drawn on the copper's top and bottom faces,
        length ~ sqrt(|J|) relative to the peak."""
        p = self.plotter
        mags = [lr.arrow_mag for lr in scene.layers.values() if len(lr.arrow_mag)]
        if not mags:
            return
        # Scale against the 95th percentile, not the peak: the peak sits at a pad corner
        # and would shrink every other arrow to its minimum length.
        peak = float(np.percentile(np.concatenate(mags), 95))
        if peak <= 0:
            return
        for lid, lr in scene.layers.items():
            if not len(lr.arrow_pts):
                continue
            lengths = lr.arrow_spacing * (0.3 + 0.65 * np.minimum(1.0, np.sqrt(lr.arrow_mag / peak)))
            z_levels = [lr.arrow_pts[0, 2]]
            if lr.arrow_z_below is not None:
                z_levels.append(lr.arrow_z_below)
            arrows = flat_arrows(lr.arrow_pts, lr.arrow_dir, lengths, z_levels)
            # Unlit, so the arrows stay plain white at any viewing angle.
            actor = p.add_mesh(arrows, color="white", lighting=False, name=f"arrows:{lid}",
                               pickable=False, reset_camera=False, render=False)
            actor.SetScale(1.0, 1.0, self._zscale)
            self._arrow_actors[lid] = actor

    def _draw_copper_result(self, scene, arr, assoc, title):
        """Copper colored by a field; vias drawn plain for context."""
        p = self.plotter
        lo, hi, log_scale = _copper_clim(scene, self._result_field)
        lo, hi = apply_limits(lo, hi, log_scale, self._color_limits)
        self._clim = (lo, hi)
        cmap = "turbo" if assoc == "point" else "inferno"
        bar =_bar_args(title, _bar_format(lo, hi, log_scale))
        first = True
        for lid, lr in scene.layers.items():
            actor = p.add_mesh(lr.mesh, scalars=arr, preference=assoc, cmap=cmap,
                               clim=(lo, hi), log_scale=log_scale, nan_color="#555555",
                               smooth_shading=False,
                               show_scalar_bar=first, scalar_bar_args=bar,
                               name=f"result:{lid}", reset_camera=False, render=False)
            actor.SetScale(1.0, 1.0, self._zscale)
            self._apply_result_opacity(actor)
            self._result_actors[lid] = actor
            first = False
        self._style_bar(title)
        if scene.vias is not None:
            self._result_via_actor = p.add_mesh(
                scene.vias, color=_VIA_COLOR, specular=0.6, name="result:vias",
                reset_camera=False, render=False)
            self._result_via_actor.SetScale(1.0, 1.0, self._zscale)

    def _draw_via_result(self, scene, arr, title):
        """Vias colored by current; copper drawn as muted gray for context."""
        p = self.plotter
        for lid, lr in scene.layers.items():
            actor = p.add_mesh(lr.mesh, color="#7a7f88", opacity=_VIA_VIEW_COPPER_OPACITY,
                               smooth_shading=False, name=f"result:{lid}",
                               reset_camera=False, render=False, pickable=True)
            actor.SetScale(1.0, 1.0, self._zscale)
            self._apply_result_opacity(actor)
            self._result_actors[lid] = actor
        if scene.vias is None:
            return
        lo, hi = scene.ranges["via_current"]
        if hi <= lo:
            hi = lo + 1e-9
        lo, hi = apply_limits(lo, hi, False, self._color_limits)
        self._clim = (lo, hi)
        self._result_via_actor = p.add_mesh(
            scene.vias, scalars=arr, cmap="viridis", clim=(lo, hi), specular=0.4,
            show_scalar_bar=True, name="result:vias", reset_camera=False, render=False,
            scalar_bar_args=_bar_args(title, _bar_format(lo, hi, False)))
        self._result_via_actor.SetScale(1.0, 1.0, self._zscale)
        self._style_bar(title)

    def _style_bar(self, title):
        """Tick values to the left of the bar, where they stay inside the view, and the
        title above the bar, ending at its right edge."""
        self._scalar_bars.append(title)
        bars = self.plotter.scalar_bars
        if title in bars:
            bar = bars[title]
            bar.SetTextPositionToPrecedeScalarBar()
            bar.SetVerticalTitleSeparation(12)
            # Control the spacing between tick labels and the color bar
            bar.SetTextPad(-2)
            # pyvista keys the bar by its title, so the title is kept but not drawn.
            bar.GetTitleTextProperty().SetOpacity(0.0)
            actor = _bar_title_actor(title)
            self.plotter.renderer.AddViewProp(actor)
            self._bar_titles.append(actor)

    # ------------------------------------------------------------------ views
    def _fit(self, zoom: float = 1.0):
        self.plotter.reset_camera()
        self.plotter.camera.zoom(zoom)
        self.plotter.render()

    def view_top(self):
        self.plotter.view_xy()
        self._fit(1.3)

    def view_bottom(self):
        self.plotter.view_xy(negative=True)
        self._fit(1.3)

    def view_iso(self):
        """Three-quarter view from the front-left, looking down at about 40 degrees."""
        self.plotter.view_vector((-0.45, -0.75, 0.75), viewup=(0, 0, 1))
        self._fit(1.4)

    def screenshot(self, path: str):
        self.plotter.screenshot(path, transparent_background=False)

    # ---------------------------------------------------------------- picking
    def on_click(self, cb):
        """Register ``cb(point, screen_xy)`` for left clicks (not drags).

        ``point`` is the clicked copper surface point as (x_mm, y_mm_kicad, z_mm), or None
        if the click hit no copper; ``screen_xy`` is the click in display pixels, for use
        with :meth:`screen_distance_to_segments`.
        """
        self._pick_cb = cb

    def _event_pos(self):
        return tuple(self.plotter.iren.interactor.GetEventPosition())

    def _on_left_press(self, *_):
        self._press_pos = self._event_pos()

    def _on_left_release(self, *_):
        start, self._press_pos = self._press_pos, None
        if start is None or self._pick_cb is None:
            return
        x, y = self._event_pos()
        if abs(x - start[0]) + abs(y - start[1]) > 4:     # a drag (rotate/pan), not a click
            return
        point = None
        if self.pi_marker_at((x, y)) is not None:       # a part's name, over the copper
            self._pick_cb(None, (x, y))
            return
        if self._picker.Pick(x, y, 0, self.plotter.renderer) and self._picker.GetCellId() >= 0:
            wx, wy, wz = self._picker.GetPickPosition()
            point = (wx, -wy, wz / self._zscale)
            self._show_marker((wx, wy, wz))
        self._pick_cb(point, (x, y))

    def _show_marker(self, world_xyz):
        self.plotter.add_points(np.array([world_xyz]), color="white", point_size=12,
                                render_points_as_spheres=True, name="pick_marker",
                                pickable=False, reset_camera=False, render=False)
        self.plotter.render()

    def _to_screen(self, pts: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Display pixels (N, 2) of points (N, 3) in KiCad coords, and which of them are
        behind the camera."""
        ren = self.plotter.renderer
        m = ren.GetActiveCamera().GetCompositeProjectionTransformMatrix(
            ren.GetTiledAspectRatio(), -1, 1)
        M = np.array([[m.GetElement(i, j) for j in range(4)] for i in range(4)])
        P = np.asarray(pts, dtype=float).reshape(-1, 3).copy()
        P[:, 1] *= -1.0                       # KiCad y down -> view y up
        P[:, 2] *= self._zscale
        H = np.column_stack([P, np.ones(len(P))]) @ M.T
        behind = H[:, 3] <= 0
        ndc = H[:, :2] / np.where(behind, 1.0, H[:, 3])[:, None]
        w, h = ren.GetSize()
        ox, oy = ren.GetOrigin()
        return np.column_stack([(ndc[:, 0] + 1) / 2 * w + ox,
                                (ndc[:, 1] + 1) / 2 * h + oy]), behind

    def screen_distance_to_segments(self, segments: np.ndarray, screen_xy) -> np.ndarray:
        """Pixel distance from ``screen_xy`` to each 3D segment (N, 2, 3), KiCad coords."""
        if len(segments) == 0:
            return np.zeros(0)
        xy, behind = self._to_screen(segments.reshape(-1, 3))
        xy = xy.reshape(-1, 2, 2)
        a, b = xy[:, 0], xy[:, 1]
        ab = b - a
        p = np.asarray(screen_xy, dtype=float)
        t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
        d = np.linalg.norm(p - (a + t[:, None] * ab), axis=1)
        d[behind.reshape(-1, 2).any(axis=1)] = np.inf
        return d

    def close(self):
        self.plotter.close()
        super().close()
