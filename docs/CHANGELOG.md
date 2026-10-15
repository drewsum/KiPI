# Changelog

All notable changes to KiPI, newest first. Each minor version is one development milestone.
The code's version number went from 0.1.0 straight to 0.5.0, so 0.2.0 to 0.4.0 name
milestones rather than versions that were ever set.

## 0.7.0 (in development): AC impedance analysis, faster solves

### Added
- **AC analysis: PDN impedance over frequency.** The right end of the toolbar has
  *Mode: DC / AC*. DC is the default and works as before; AC swaps the PI setup panel for
  an AC setup, and the DC setup and results come back when DC is chosen again.
  - The AC setup shares the VRMs and series links with DC (series links gain an ESL), and
    adds capacitor models (C, ESL, ESR, assigned to parts, with *Models from values*),
    probes, the sweep and its own mesh settings.
  - A rail and its ground are field-solved as a 2.5D model with two faces per copper
    sheet: plane resistance with skin effect, spreading and via inductance, plane
    capacitance and cavity resonances. The parts are attached at every sweep frequency,
    and the copper is solved at fewer frequencies and interpolated in between.
  - The **Impedance** tab beside the 3D view plots |Z| for every probe with its target. A
    cursor tracks a curve (click, drag, arrow keys) and shows frequency, |Z|, phase, R, X,
    equivalent L or C and the target margin in the Info box.
  - Probe targets are flat up to a frequency, or a custom piecewise-linear mask. The
    *Target Impedance/Frequency Calculator* derives a flat target from the AC voltage
    tolerance, load step and slew rate.
  - Each rail keeps its last result; switching the rail shows its curves without solving
    again. *Export CSV…* writes |Z|, R and X per probe.
- **User guide and tooltips.** *Help* in the toolbar (or F1) opens `docs/help.html`, an
  offline guide to every button, the workflows and the results, with collapsible sections.
  Every button, box and table in KiPI now has a tooltip.
- **Pre-filled setup fields.** Picking pins fills in what usually follows from them, and
  every guess can be overwritten:
  - The ground pins of a VRM, load or probe, from the same part. The ground net is a
    ground the setup already uses, a net named like a ground, or else the board's biggest
    net.
  - An LDO or switcher's input pins, when its output part has exactly one other rail.
  - A series link's side B, when side A is on a two-terminal part (not a capacitor, and
    not across to ground).
  - New phases of a multi-phase VRM, on the next parts in sequence (`L1` → `L2`).
  - VRM names follow the output part, as load and link names already did.
  - The set voltage from the net name now reads negative rails (`-5V`) and leaves the
    voltage alone when the name has none, instead of setting 1 V.
- **Thermal limits.** *Max copper temperature* and *Max rise above ambient* grade an
  electro-thermal solve; a new Thermal results tab lists the hottest copper on each layer
  with PASS/FAIL.
- **Solve only some nets (restored).** The DC setup's Solve tab lists every net group with
  a checkbox, so rails that are not modeled or routed yet can be left out while the rest is
  solved. It was written for 0.6.0 but never reached a release. Only the ticked groups are
  validated, and the choice is saved with the setup. Where a solved group meets an
  excluded one:
  - A VRM whose output is excluded becomes a constant load on its input pins, drawing the
    input current its rail would need at nominal voltages.
  - A VRM whose input is excluded is treated as an ideal source.
  - Ground pins on an excluded ground are ignored.
  - The Notes tab lists what was left out and these substitutions.
- **Optional multithreaded solver.** If `pypardiso` is installed (Windows and Linux on
  x86-64), KiPI solves with Intel MKL's PARDISO, and directly for rails of up to 1M
  unknowns. A 645k-unknown rail solves in 1.2 s instead of about 8 s.
  - It is opt-in because of its size. MKL and its libraries are about 210 MB to download
    on Windows (about 320 MB on Linux) and about 630 MB on disk. That takes KiPI's plugin
    environment from about 860 MB to about 1.5 GB. The README explains how to add it.
  - Without it, or if it fails on a system, KiPI falls back to SciPy.
  - *Copy version info* names the solver in use.

### Changed
- **Faster solves.** Rails of up to 200k unknowns now get a direct solve rather than AMG,
  which on board meshes is 2–3× faster and exact, and makes later power-tree passes nearly
  free. The example board's power tree solves in 1.7 s instead of 3.0 s.
- **Faster electro-thermal runs on large boards.** Each iteration reuses the previous
  iteration's AMG hierarchy as the preconditioner rather than building a new one, because
  only the copper's resistivity has changed (89 s → 78 s for four iterations at 645k
  unknowns; 34 s with PARDISO).

### Fixed
- **Result colors no longer flicker on coincident copper.** Overlapping copper surfaces
  z-fought in the 3D view, so DC results showed speckled patches; they now render
  smoothly.
- Saving results after an electro-thermal solve failed ("Can't pickle local object").
  Saved problems now leave out the live temperature field, keeping its effect on
  resistivity and the temperature map itself.
- **The window fits a 1080p screen again.** Its minimum size had grown to 1764 × 1106 px,
  so maximized on a 1080p screen its bottom was cut off, and dragging the log taller pushed
  more of it off the screen. The VRM, load and other setup editors now scroll instead of
  setting the window's height, and the impedance plot's hint text wraps. The minimum is
  now about 1280 × 790.

### Development
- CI also runs the tests on Windows with PARDISO installed.
- **Package restructured for more than one analysis.** Code shared by every analysis
  (board model and extraction, geometry, meshing, sparse solvers, copper views) moved to
  `kipi.core`; the DC analysis (power tree, conduction solver, thermal, reports, result
  views, setup panel) moved to `kipi.dc`; the AC analysis is in `kipi.ac`. The
  direct / AMG-CG solver moved out of the DC FEM into `kipi.core.solver.spd`. Results
  files saved by earlier versions still open (`kipi.compat` maps the old module paths).

## Merged after 0.6.5 (2026-10-14): Regulation checks and copper shapes

Merged to main as PR #39 (contributed by CJ Clark) without a version number of its own;
it ships with 0.7.0.

### Added
- **LDO dropout and VRM input range.** Beside the output current limit, a VRM takes a
  dropout (LDOs) and an optional minimum and maximum input voltage. A VRM that cannot hold
  its set point marks every load it feeds, directly or downstream, **VRM OUT OF
  REGULATION** (FAIL). A multi-phase VRM is judged on its worst phase.
- **Copper graphic shapes are copper.** Lines, arcs, circles, rectangles, polygons and
  Béziers on a copper layer that carry a net, on the board or inside a footprint, are
  solved as filled copper. Shapes with no net are left out, and a warning counts them.

### Changed
- **LDOs are checked against their input.** An LDO whose input sits below its output plus
  its dropout now fails its loads. With the default dropout of 0 V, a part modelled as an
  LDO that passes a lower voltage through (a load switch, say) now fails where it passed
  before. Model such parts as a DC connection instead.
- **One verdict for each load** feeds the results table, the CSV export and the HTML
  report, so the three always agree, including the new VRM OUT OF REGULATION and NOT
  CONVERGED.
- **Stricter setup checks.** Negative quiescent current, dropout or DC connection
  resistance, and an input range whose minimum is above its maximum, are setup errors.

### Fixed
- **Loads on a rail that did not converge** could read PASS. They now read **NOT
  CONVERGED** and are not judged.
- **The board's stackup was ignored on KiCad 10.0.1**, which leaves every layer's
  enabled flag unset. A default 1.6 mm stackup with 17.5 µm inner copper replaced the
  board's own. Disabled layers are now skipped only when KiCad reports the flag.
- **Electro-thermal iteration 1** solved the copper at the 20 °C reference rather than at
  ambient.
- The log no longer reports the ground sense as converged when it was not.

### Development
- `solve_tree` is split into `_TreeSolver`, one method per step. The result classes moved
  to `pi/results.py` and can still be imported from `pi/tree.py`, so saved results open.
- The kicad-python geometry converters moved to `kicad_io/convert.py`, shared by the board
  reader and the new copper-shape reader (`kicad_io/copper_shapes.py`).

## 0.6.5 (2026-10-13): Drop and boost, limits and fixes

### Added
- **Drop and boost.** A load can sit above its VRM's set point as well as below it: with
  remote sense, the VRM raises its output pins to hold a sense point further down the
  copper. Results now report each deviation as a drop or a boost, and the worst pin is the
  one furthest from the set point either way.
  - The load's PASS/FAIL limit is now a **Tolerance** (± %) rather than a maximum drop.
  - The probe, Loads table, CSV export and HTML report all say "drop" or "boost".
  - The loads CSV gives each deviation as a magnitude plus a `drop`/`boost` column, and
    adds the lowest and highest pin voltages.
- **VRM output current limit.** An optional limit per VRM, covering all its phases, with
  PASS/FAIL in the VRMs results tab, the CSV export and the HTML report.
- **Sortable results tables.** Click a column header to sort by it, and click again to
  reverse. Numbers sort by value, and each multi-phase VRM keeps its phase rows under it.
- **Resistor values for DC connections.** A new DC connection starts on a part that bridges
  the two nets, such as a sense resistor. When both sides are on the same resistor, its
  resistance is read from its value field, e.g. `10m`, `4k7`, `0R1` or `5 mΩ`.
- **Ticked nets are saved with the setup.** The nets ticked in the Board panel are restored
  when the board is next opened. **Load from KiCad** on a board that is already open keeps
  the current ticks. Only a board with no saved choice starts with a guess.
- **About dialog** (toolbar), with the version, author, project links and the versions of
  the libraries KiPI is running on, ready to copy into a bug report.
- **KiPI's own taskbar entry.** On Windows, macOS and Linux, KiPI shows with its name and
  icon instead of as the Python interpreter that KiCad starts it with.
- **Progress messages** before long steps: meshing large planes, joining layers through
  many plated holes, mapping copper for the thermal model, and solving the board
  temperature.
- **KiCad 9 support**, confirmed on KiCad 9.0.7. Boards are read from KiCad 9.0.0 onwards.
  Where a KiCad 9 release lacks a request, KiPI takes the layers and their names from the
  stackup instead, and zone refills wait on a request KiCad 9 answers. Before 9.0.3, plated pads are kept on
  every layer, because KiCad cannot say which layers have their copper.
- **README badges** for the supported KiCad versions, platforms and Python versions.
- **Install notes for macOS and Linux:** plugin folders, and which Python each platform's
  KiCad runs plugins with (macOS needs a newer Python than the one KiCad bundles).

### Changed
- **The color scale fits the layers shown.** Hiding a copper layer rescales the result
  colors to the layers left, so one layer's outlier no longer flattens the rest.
- **Color bar labels** sit to the left of the bar, below the title, with enough decimals to
  tell the ticks apart (a 3.3 V rail that sags 0.4 mV no longer reads "3.3" on every tick).
- **Faster meshing of boards with many through-hole pads.** Each layer's drill holes are
  merged once, rather than once per pad.
- **The watchdog** dumps thread stacks from Python rather than from faulthandler's timer, and
  logs the UI thread's stack if the window stops responding during a task.
- **kicad-python 0.7.1 or later** is required. 0.7.0 was only published as a source package
  that does not build.
- Net codes are read without kicad-python's deprecated `Net.code`, which KiCad is dropping
  from the API.

### Fixed
- **Refill zones** could return before KiCad had finished filling, so the reads that followed
  failed with `AS_BUSY` or timed out. KiPI now waits until the fill has finished.
- **Electro-thermal and large solves could hang or crash.**
  - The faulthandler timer could kill a solve that was still running. Stack dumps now come
    from a Python thread instead.
  - Conjugate gradients could diverge at the round-off floor on large boards. It now stops
    when it stops improving and keeps its best result, and falls back to a direct solve if
    that result is not good enough.
  - Warm-started re-solves that already meet the tolerance skip CG entirely.
- **Changing the via current limit** froze the window for up to a minute on large boards.
  PASS/FAIL now updates in place.
- **Loads on negative rails** always passed. Tolerance and drop percentages now use the size
  of the set point.
- **"Show copper"** had no direct way back to the results. Picking the same group again,
  changing the result field, or double-clicking a via row now shows them.
- **Open results…** crashed when no board was loaded. It now asks for the board first.
- **Clicking a pad** in the results of a group that joins several nets did not identify it.
- **Slotted plated holes** were cut along the board's x or y axis, ignoring the pad's
  rotation, so a rotated slot could cut through copper outside its pad. Pad rotation is now
  read from KiCad and saved in snapshots. Snapshots from older versions take the slot's
  direction from the long axis of the pad's copper.
- **Terminal currents in power-tree results.** VRM input, phase and ground-return terminals
  reported 0 A, so per-group KCL and energy-balance checks were wrong. They now report the
  currents the solve used. No result shown in the GUI or the report was affected.
- CI runs on every branch, whatever its name, and the window tests run on Linux without an
  OpenGL context.

### Development
- **Tests against every KiCad API version.** A fake KiCad answers the IPC API's own
  messages as each release from 9.0.0 to 10.0.6 does, and the tests extract a board from
  each one through kicad-python's real `Board`.
- **CI covers more platforms:** macOS, Python 3.10 to 3.13 on Linux, and the oldest allowed
  kicad-python. A job that does not block CI shows whether every dependency has prebuilt
  packages for Python 3.14 yet.

## 0.6.0 (2026-10-12): Quality of life

### Added
- **Multi-phase VRMs.** A VRM can have N parallel phases, each with its own output, input
  and ground pins and one shared pair of sense pins. Each phase carries 1/N of the output
  current and settles at whatever voltage its own copper gives it. The VRM regulates its
  sense pins or, without them, the average of its phase outputs. Each phase draws its own
  input current and returns its own ground current.
  - The VRMs results tab, the HTML report and a new `<board>_vrm_phases.csv` export list
    every phase's voltages, currents and loss.
  - In the thermal co-simulation, each phase's conversion loss is placed at its own output
    pins.
  - Validation checks that phases share rails and grounds but not pins.
  - Setups and results files saved by older versions still load, as single-phase VRMs.
- **Negative remote sense.** Optional ground sense pins: the VRM holds its set voltage
  relative to them instead of 0 V.
- **Per-layer copper resistivity.** The Copper tab lists every layer with its own resistivity,
  overriding the board default, for example for electroplated or rolled-annealed foil.
- **Copper thickness unit.** Layer thicknesses can be shown in µm, mil or oz. This only
  changes the display, and the choice is remembered.
- **Example board.** `examples/multiphase_buck` is a 4-phase 12 V to 0.9 V, 40 A core rail,
  with its snapshot, PI setup and the script that generates them.
- **Linting.** ruff runs in CI. It is a development dependency only and is never installed
  into KiCad's plugin venv.
- **Tests.** The tests solve the example board and check that its committed setup still
  matches its generator.
- **Changelog.** This file.

### Changed
- American spelling throughout the user interface, messages and documentation (color,
  analyze, center, gray, and so on).
- The README explains what the 3D voltages are measured against, and why a plane can read
  above its set point when ground returns are solved.

### Fixed
- Ticking or unticking nets in the Board panel could overwrite the VRM or load being
  edited with a different net and voltage. For example, a source VRM could end up driving
  another VRM's output.
- Startup could crash on Windows (0xC000041D) when the crash handler tried to dump the
  stacks of threads that were still running.
- Unused imports, an unhelpful exception chain in the mesher and other issues found by ruff.

## 0.5.0 (2026-10-11): Electro-thermal co-simulation and HTML report

### Added
- **Electro-thermal co-simulation** (Thermal tab, off by default).
  - A board-level heat model: in-plane conduction through every layer's copper and the
    laminate, and convection from both faces with an airflow setting.
  - Each iteration solves the power tree with temperature-dependent copper resistivity,
    then solves the board temperature from all the heat: copper, vias, DC connections and
    VRM conversion loss.
  - A Temperature result view, with the peak temperature and the iteration history.
- **Laminate conductivity from the stackup.** Dielectric material names are matched to
  typical thermal conductivities, covering FR4 glass styles and resin content, high-Tg FR4,
  polyimide, PTFE, Rogers, ceramics and metal cores. Unrecognized names fall back to FR4 with
  a warning, and **Laminate k** overrides the result.
- **HTML report.** A single offline file containing the summary and every results table, an
  interactive 3D view per net group, per-layer statistics with top-down voltage and
  current-density maps, and the thermal map.
- Package metadata in `pyproject.toml`, a `kipi` command-line entry point, the GPL-3.0-or-later
  license, the README user guide with screenshots, and the KiPI logo.

### Changed
- Repeated solves warm-start the AMG solver and reuse cached meshes.

## 0.4.0 (2026-10-11): Results tools

### Added
- **Current-flow arrows** in the current-density view.
- **Cut planes** along X, Y or Z, with a position slider and a flip option.
- **Vias tab.** Every via and PTH barrel ranked by current, with an optional current limit
  for PASS/FAIL. Double-click a row to fly to the via.
- **Losses tab.** Copper loss per group and layer, plus vias, DC connections and VRM
  conversion.
- **Exports.**
  - CSV tables.
  - ParaView `.vtm` files per group.
  - Saving and reopening `.kipi-results` files without solving again.
- **Load pin groups.** A load can split its current unevenly across groups of pins.
  Results report the worst pin, and PASS/FAIL judges it.

## 0.3.0 (2026-10-11): Power tree

### Added
- **PI setup.** Define VRMs, loads and DC connections, each referenced by part and pad.
  - VRMs can be sources, LDOs or switchers, with efficiency and quiescent current.
  - VRMs can have remote sense pins and ground pins.
  - Loads have supply and ground pins, a current and a drop tolerance.
  - The setup is saved next to the board as `<board>.kipi-setup.json` and reloaded with it.
  - Live validation keeps **Solve all** disabled until the setup is valid.
- **Power-tree solver.** Rails are solved downstream first, and each VRM's input current
  becomes a load on the rail that feeds it. Switcher input current is iterated until it
  converges. Ground returns are solved too.
- **DC connections.** Series parts such as resistors, ferrites and 0 Ω links join nets.
- **Results tables.**
  - Loads: drop, %, PASS/FAIL.
  - VRMs: voltage, current and power in and out, and loss.
  - DC connections: current and dissipation.
- **Searchable pickers.** Type part of a net, reference or value to filter the net and part
  pickers.

### Changed
- The single-net solve panel is replaced by the PI setup panel.
- The linear system is factorized once and reused across power-tree iterations.

## 0.2.0 (2026-10-11): DC solver

### Added
- **Mesher.** A conforming triangle mesh per net and layer, with pad outlines and plated-hole
  rings, refined towards holes.
- **2.5D FEM DC solver.** Sheet conductance per layer plus via and PTH barrel resistance. A
  direct solve is used for small boards and AMG-preconditioned CG for large ones. Results
  are checked against closed-form cases.
- **Result views.**
  - Voltage, current density, power density and via current.
  - Click any copper point or via to probe it.
  - Vias are picked by on-screen distance.
- **Logs.** Per-session logs, with crash tracebacks and a watchdog that dumps every thread's
  stack during long tasks.

### Fixed
- Hangs when KiCad stopped reading the plugin's output, near-endless mesh refinement on pad
  slivers, and a Triangle crash on unnoded geometry.

## 0.1.0 (2026-10-11): Board extraction and 3D viewer

### Added
- **KiCad 10 IPC plugin** (`plugin.json`) that extracts the stackup, nets, tracks, arcs,
  vias, pads, footprints, zone fills and board outline.
- **Snapshots.** JSON and gzip board snapshots for working without KiCad. Open one with
  `python -m kipi --snapshot`.
- **3D copper viewer.**
  - Copper is drawn at its true heights, with via barrels, pads and the board body.
  - Layer and net toggles, Z exaggeration, and picking with select-in-KiCad.
  - Nets render as they are ticked.
  - Meshes are batched so boards with hundreds of nets stay responsive.
- **Development.** CI runs the tests on Windows and Linux with Python 3.11, and research
  notes are in `docs/research.md`.
