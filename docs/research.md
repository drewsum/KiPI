# KiPI research notes (2026-10)

## Reference: KiPIDA (kbralten/KiPIDA, AGPL-3.0)

| Area | KiPIDA approach | Limitation |
|---|---|---|
| KiCad access | kipy IPC, hand-built socket path, `kicad_version: 9.99` | Hoop-jumping on KiCad 10; kipy already reads `KICAD_API_SOCKET`/`KICAD_API_TOKEN` |
| Mesh | Uniform raster grid (default 0.5 mm) via matplotlib `Path.contains_points` | A 0.5 mm grid cannot resolve 0.15–0.3 mm traces; refining blows up node count |
| Conductance | Edge g = t/ρ (sheet conductance); via = single-node annular barrel resistor | Vias/pads are point contacts → current crowding is not captured |
| Assembly | COO stamps, but a per-edge Python loop | Slow on large boards |
| Sources | Row overwrite for Dirichlet nodes | Breaks symmetry → can't use CG/Cholesky |
| Solve | `pypardiso` or `scipy.sparse.linalg.spsolve`, a single blocking call | No progress or feedback during the solve |
| Power tree | Leaf-to-root; LDO passes current 1:1, SMPS by efficiency | Good model, keep |
| Output | Matplotlib heatmaps per layer | Not true 3D, can't rotate/zoom/probe |

## Chosen engine: 2.5D P1 triangular FEM

The same approach as atx/padne (GPL-3.0). Its FEM core is about 40 lines on SciPy.

* **Geometry.** Per net and layer, take the shapely union of buffered tracks and arcs, pad polygons
  (`GetPadShapeAsPolygon`), via rings and zone `filled_polygons`, minus drill holes.
  This is implemented in `kipi/geometry/copper.py`.
* **Mesh.** Use `triangle` (Shewchuk; win/cp311 wheels exist) with quality and max-area constraints,
  refined near pads, vias and narrow necks. Conforming triangles resolve thin traces cheaply
  and stay coarse in planes.
* **Element.** Sheet conductance σt. The P1 stiffness is σt·(bbᵀ+ccᵀ)/(4A), vectorized in numpy.
* **Vias / PTH.** Punch the drill hole in each layer mesh and tie the hole-edge nodes to a barrel
  node with the barrel resistance ρh / (π(d·tp − tp²)).
* **Pads.** Each pad is an equipotential terminal region (star coupling, as in padne).
* **Sources.** Eliminate Dirichlet nodes properly to keep the system SPD. Loads become current
  injections. DC series parts (R, ferrite, fuse) become lumped conductances. Nets joined by
  series parts are solved as one "extended net", so the drop through the part and the copper
  on both sides of it come out of one solve.
* **Solver.** `pyamg` smoothed-aggregation preconditioned CG with a per-iteration callback
  that reports the residual live. SuperLU (or optional pypardiso) is the fallback.
* **Post-processing.** Per triangle, J = −σ∇V and P = |J|²/σ · t · A. Via current = g·ΔV. Also
  per-pin currents and load drop vs. spec.
* **Electro-thermal (later).** Use ρ(T) = ρ₀(1 + α(T−T₀)) with fixed-point iteration.

## KiCad 10 IPC API facts (verified against KiCad 10.0.7 + kicad-python 0.8.0)

* Plugins live in `${KICAD_DOCUMENTS_HOME}/10.0/plugins/<dir>/plugin.json`.
  If the Documents folder is redirected to a synced folder, the plugins folder moves with
  it, so go by `KICAD_DOCUMENTS_HOME` rather than the default path.
* `runtime.type = python`. KiCad creates `%LOCALAPPDATA%\KiCad\10.0\python-environments\<identifier>`
  with `--system-site-packages` on its bundled Python 3.11 and pip-installs `requirements.txt`.
  numpy, shapely and wx already exist in the base interpreter.
* The schema is checked with `python -m kipy.packaging validate .`.
* All coordinates are in nm. Pad positions from `FootprintInstance.definition.pads` are absolute
  board coordinates (verified: 0 mismatches against pad-shape centroids on a 355-pad board).
* `Board.get_pad_shapes_as_polygons(list)` **drops pads with no shape**, which misaligns its
  result with the input list. KiPI sends `GetPadShapeAsPolygon` directly and matches by pad id.
* `check_padstack_presence_on_layers` handles unconnected-layer removal on inner layers.
* `Zone.filled_polygons` gives `{layer: [PolygonWithHoles]}`. `Zone.is_rule_area()` is a method.
* Zone fills come back **fractured**. Every antipad or clearance hole is joined to the outer ring
  by a zero-width slit, so `holes` is empty and shapely reports a "Ring Self-intersection".
  `shapely.make_valid` restores real interiors (647 on the Audio_Visualiser_DSP board) and
  leaves the slits as `LineString`s, which `geometry/copper.py` drops. The solver mesher must
  run on the repaired polygons, not the raw rings.
* Footprints can contain unnumbered, net-less pads with no copper (paste or mask apertures,
  e.g. 12 on a wafer-level chip-scale package). KiCad reports no copper shape for them, which is correct.
* `get_stackup()` gives copper and dielectric thicknesses. Dielectric layers can have sub-layers.
* Requests go through KiCad's UI thread, so expect `AS_BUSY` and retry. Use one connection per process.
* From 10.0.1, plugin stdout/stderr shows in the KiCad status bar.

## KiCad 9 compatibility (confirmed on KiCad 9.0.7)

KiCad 9 runs the same IPC plugins (`plugin.json`, `KICAD_API_SOCKET` / `KICAD_API_TOKEN`)
and speaks the same protobuf API, but some requests arrived during 9.0.x. The kicad-python
version does not matter: it is only the client, and the newest one works with an older
KiCad. What matters is whether that KiCad has a handler for each request.

| Request KiPI sends | First KiCad | What KiPI does on an older one |
|---|---|---|
| `GetNetClassForNets` | 9.0.1 | Leaves net classes empty |
| `CheckPadstackPresenceOnLayers` | 9.0.3 | Keeps pads on every layer, and warns |
| `GetBoardEnabledLayers` | 9.0.5 | Takes the copper layers from the stackup |
| `GetBoardLayerName` | 9.0.8 | Takes the names from the stackup's `user_name`, then the canonical names |
| `GetItems` for `KOT_PCB_GROUP` (refill busy probe) | 10.0.0 | Asks for text, then tracks |

* A request with no handler comes back as `AS_UNHANDLED` with the message "no handler
  available for request of type kiapi.board.commands.<Name>"
  (`KICAD_API_SERVER::handleApiEvent`, `common/api/api_server.cpp`). `_unsupported()` in
  `extract.py` checks for either, and only then falls back. Any other error is raised.
* KiCad checks for busy before it looks at the item types, so the refill probe still sees
  `AS_BUSY` on KiCad 9.
* The first KiCad for each request is taken from kicad-python's `versionadded` notes.
  `tests/kicad_fake.py` encodes this table (`ADDED`), and `tests/test_kicad_versions.py`
  extracts a board from every version listed there.

## GUI / 3D

* KiCad 10 plugins are separate processes, so the GUI toolkit is not tied to wx.
* PySide6 (`PySide6-Essentials` keeps the venv smaller) + `pyvistaqt.QtInteractor` embeds VTK
  with full rotate/zoom/pick. Other plugins on this machine (fanout-tool, OrthoRoute,
  KiCad-Parasitics) already install PySide6/PyQt6/pyvista into their KiCad venvs.
* Copper is drawn as slabs at true stackup Z with a Z-exaggeration scale. Vias are drawn as barrels.
* Later additions: scalar fields (V, ΔV, |J|, P), J arrow glyphs, clipping planes, `.vtu` export.

## Solver validation and accuracy findings (Milestone 2)

* **Straight trace.** Matches rho*L/(W*t) to 1e-9. P1 elements are exact for a linear field.
* **Radial spreading into a via.** With a uniform mesh the error was -1.7% at h = 0.4 mm. A
  P1 model with equipotential ends over-estimates conductance, so the error was in the
  expected direction, but it converged only as O(h). The cause is the 1/r field near the
  drill. A size field that grows linearly with distance from each plated hole,
  h = clamp(h_min + k*d, h_min, h_max), brings it to -0.02% for about the same node count.
  The defaults are k = 0.2 and h_min = 0.02 mm.
* **Drill polygons.** Approximating a drill as an 8-gon makes it look about 0.8% more
  resistive than a circle, because an octagon's effective radius is smaller. The default
  is 16 segments.
* **Solve for the offset from the source voltage.** A rail drop is about 1e-4 of the rail
  voltage. Solving in absolute volts lets the source term dominate the right-hand side, so a
  relative residual of 1e-10 said little about the drop: AMG and direct solves disagreed by
  0.4%, and the energy balance error was about 1e-6. Solving for u = V - V_ref gives energy
  balance and KCL errors of about 1e-10, with AMG matching direct to five digits.
* **Plated holes.** A plated hole ties its ring together even when it has copper on only one
  layer, because the barrel wall shorts the hole edge. Dropping single-layer barrels left
  1 mm PTH holes in a plane acting as insulating cut-outs and shifted load drops by 0.4%.
* **Dead rings.** Annular rings that touch nothing but their own barrel carry no current. On
  the DSP board, skipping them removes 29% of the nodes with an identical answer.
* **Performance.** On the DSP board, +3.3V meshes to 174k nodes on 3 layers. Smoothed-
  aggregation AMG-CG then converges in about 60 iterations, and the whole run takes 2–3 s.

## Robustness findings (hangs and crashes seen in KiCad)

* **KiCad's stdout/stderr pipe can block the plugin forever.** KiCad captures a plugin's
  output through a pipe and doesn't always drain it. Once the few-KB buffer fills, the next
  write blocks, freezing whichever thread was logging. A long solve logs CG progress, so the
  first solve worked and a later one hung at "Solving…". Now logging echoes only to an
  interactive terminal, and the plugin entry redirects fds 1 and 2 to a per-session
  `.console.log`. `tests/test_app.py` reproduces the problem with an unread pipe.
* **Triangle and nearly coincident constraints.** If a pad outline runs within a few µm of the
  (simplified) copper edge, it creates hair-thin slivers, and quality refinement then splits
  them essentially forever (+12Vin, J201). Pad outlines are now clipped 10 µm clear of copper
  edges and extended straight to the edge. A Steiner-point cap (`S`) guarantees termination.
* **Triangle segfaults on unnoded input.** Noding in floating point and rounding afterwards
  can leave segments crossing without a shared vertex, and Triangle crashes the whole process
  on that (+3.3VA_DSP). Noding now uses GEOS snap-rounding on the 1 nm KiCad grid, and every
  input is checked (`pslg_problems`) before it reaches Triangle; a bad input falls back or
  raises `MeshError`, never a segfault. Meshing all 237 nets of the DSP board, with every pad
  as a terminal, now runs clean in 20 s.
* **Stale task signals.** A task's `finished` can arrive after its `done()` has started the
  next task. Handlers now ignore finishes from tasks that are no longer current.


## Electro-thermal model (Milestone 5)

* **Model.** A 2D finite-difference board model:
  * In-plane sheet conductance is Σ k_cu·t·(copper coverage of each layer, all nets) +
    k_FR4·t_board.
  * Both faces convect to ambient at h = 10 + 3.8·v W/m²K.
  * Through the thickness the board is treated as one sheet. For 1.6 mm of FR4, t/k is about
    0.005 K·m²/W, roughly a tenth of 1/(2h).
  * The model is factorized once and re-solved each iteration.
* **Tests.** Uniform heating matches q/(2h). A point source balances to 1e-9. More airflow
  runs cooler. Hot copper's drop scales exactly by 1 + α·ΔT.
* **DSP board** (2 A on +3.3V through a switcher at 85%): 1.42 W total, mostly the switcher's
  conversion loss. The peak is 82 °C at the switcher in still air and 74.7 °C at 2 m/s. It
  settles in 2–3 iterations.
* **Warm-started CG.** pyamg measures its tolerance against the first residual. After a
  warm start that residual is already small, so the tolerance has to be rescaled to
  tol·‖b‖/‖r₀‖, and the solve skipped when the warm start already meets it. Without that,
  re-solves ran to the iteration limit and fell back to direct solves (114 s → 52 s for
  four iterations).
