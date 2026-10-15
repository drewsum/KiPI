<p align="center">
  <img src="resources/kipi.gif" alt="KiPI logo" width="128">
</p>

# KiPI — DC and AC power integrity for KiCad

[![CI](https://github.com/drewsum/KiPI/actions/workflows/ci.yml/badge.svg)](https://github.com/drewsum/KiPI/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![KiCad 9.0+](https://img.shields.io/badge/KiCad-9.0%2B-314cb0.svg?logo=kicad&logoColor=white)](#requirements)
[![Platform: Windows | Linux | macOS](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)](#requirements)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10%E2%80%933.13-3776ab.svg?logo=python&logoColor=white)](#requirements)

KiPI is a power integrity analyzer that runs as a KiCad 9 and 10 IPC plugin. It has two
analysis modes:

* **DC: voltage drop.** Define voltage regulators (VRMs), loads (current into pins) and series
  parts (beads, resistors, load switches), then simulate the voltage drop net by net, across
  every copper layer. Inspect voltage, current density, via currents, copper power
  dissipation and (optionally) board temperature in a rotatable 3D view, with PASS/FAIL
  checks for loads, VRMs, vias and temperature, and write an interactive HTML report.
* **AC: PDN impedance.** Give the decoupling capacitors RLC models and put probes on a
  device's power pins, then sweep the impedance of the power distribution network (planes,
  vias, mounting and capacitors) over frequency. Check it against a flat or custom target
  impedance, with a calculator that works the target out from the device's transient spec.

New to KiPI? The **[user guide](docs/help.html)** (also the **Help** button in KiPI, or
**F1**) walks through every button, the workflows and what the results mean, and every
control in KiPI has a tooltip.

![KiPI main window: nets and layers on the left, the board in 3D, the PI setup on the right](docs/images/overview.png)

**Status: 0.7.2 (alpha)** ([changelog](docs/CHANGELOG.md)). This entire project was vibe coded with Claude Opus 5.5. I make no assurances that any of this works or is accurate; however, for my use cases (decently complex PCB layouts, windows 11 machine, KiCad 10.0.7), it seems stable. The solver is a 2.5D finite element model of the copper, checked against closed-form results (the resistance of a straight trace, current spreading radially into a via). Solving the field is the well-understood part. The modelling of vias, power supplies and the bridging of layers is where the approximations are. Please leave any comments, concerns or bugs as a github issue. I wanted a free, open source power integrity tool for KiCad, which I think this project accomplishes. I'm open to suggestions for improvements.

## Install

### Requirements

* **KiCad 9.0 or later.** Developed on KiCad 10.0.7 and confirmed on KiCad 9.0.7. The tests
  also check the API of every release from 9.0.0 to 10.0.6. On KiCad 9, KiPI works around
  the requests that release does not have yet, with two limits on the oldest releases:
  * Before 9.0.3, KiCad cannot say which layers a plated pad has copper on, so pads are kept
    on every layer, including inner layers where KiCad removed unconnected pads. KiPI
    warns about this in its log.
  * KiCad 9.0.0 cannot report net classes.
* **Python 3.10 to 3.13**, the one KiCad runs plugins with. KiCad installs KiPI's
  dependencies with pip, and some of them (Triangle, PyAMG) have no prebuilt packages for
  Python 3.14 yet.
  * **Windows:** KiCad's own Python 3.11. Nothing to do.
  * **Linux:** KiCad uses the system Python. Ubuntu 22.04 and 24.04, Debian 13 and Fedora 42
    work. Ubuntu 26.04 and Fedora 43 ship Python 3.14: until those packages exist, pip has
    to compile them, which needs a C/C++ compiler and the Python headers
    (`sudo apt install build-essential python3-dev`).
  * **macOS:** KiCad bundles Python 3.9, which is too old. Install Python 3.12 (for
    example from python.org or Homebrew) and select it as the **Python interpreter** in
    *Preferences → Plugins*.

### Steps

1. In KiCad, enable **Preferences → Plugins → Enable KiCad API**.
2. Link (or copy) this repo into your KiCad plugins folder. The examples use KiCad 10's
   folder: for KiCad 9, put `9.0` in place of `10.0`. On Windows, KiCad uses your
   *Documents* folder, which may be redirected to OneDrive:

   ```powershell
   $plug = "$([Environment]::GetFolderPath('MyDocuments'))\KiCad\10.0\plugins"
   New-Item -ItemType Junction -Path "$plug\KiPI" -Target "<path to this repo>"
   ```

   On macOS the folder is `~/Documents/KiCad/10.0/plugins`, and on Linux
   `~/.local/share/kicad/10.0/plugins`:

   ```sh
   ln -s "<path to this repo>" ~/Documents/KiCad/10.0/plugins/KiPI          # macOS
   ln -s "<path to this repo>" ~/.local/share/kicad/10.0/plugins/KiPI       # Linux
   ```

   Or, without git, download `KiPI-<version>.zip` from the
   [latest release](https://github.com/drewsum/KiPI/releases/latest) and unzip it into the
   plugins folder, which gives `plugins/KiPI`.

3. Restart the PCB editor. KiCad builds the plugin venv in the background; the first time this
   downloads PySide6 and VTK, so give it a few minutes. The **KiPI** toolbar button appears
   once the venv is ready. If it never appears, right-click the action in
   *Preferences → Plugins* and choose *Recreate Plugin Environment*.

### Optional: faster solves on large boards (Windows and Linux, x86-64)

KiPI solves each rail with SciPy's single-threaded sparse solver, or with AMG when a rail
has more than 200k unknowns. If the optional [`pypardiso`](https://pypi.org/project/pypardiso/)
package is installed, KiPI uses Intel MKL's multithreaded PARDISO solver instead, and for
rails of up to 1M unknowns. On a 645k-unknown rail one solve drops from about 8 s to 1.2 s,
and a four-iteration electro-thermal run from about 90 s to 34 s. Small boards gain little.

It is not installed by default because of its size, and because there are no MKL packages for
macOS or ARM:

| | Without PARDISO | With PARDISO |
|---|---|---|
| Extra download | | about 210 MB on Windows, about 320 MB on Linux |
| Plugin environment on disk (Windows) | about 860 MB | about 1.5 GB |

Almost all of that is MKL itself (565 MB installed) and Intel's OpenMP runtime (61 MB).
To add it, install it into KiPI's plugin environment and restart the PCB editor:

```powershell
# Windows (KiCad 10; use 9.0 for KiCad 9)
& "$env:LOCALAPPDATA\KiCad\10.0\python-environments\com.github.drewsum.kipi\Scripts\pip" install pypardiso
```

On Linux the environment is usually under `~/.cache/kicad/10.0/python-environments/`. Use its
`bin/pip` the same way. *About → Copy version info* shows which solver KiPI is using. If
PARDISO cannot load or fails on a system, KiPI falls back to SciPy and says so in the log.
To compare the two, set `KIPI_DIRECT_SOLVER=superlu`. *Recreate Plugin Environment*
removes `pypardiso`, so install it again afterwards.

## Using KiPI

### 1. Open the board

Open your board in the PCB editor and click the **KiPI** toolbar button. KiPI opens in its
own window and reads the board through the KiCad API; the log at the bottom streams each
stage as it runs. A welcome screen with the version and package versions shows for a few
seconds while it starts, and closes on its own.

KiPI runs one window at a time: clicking the button again brings the open KiPI to the
front. It also needs exactly one KiCad open, so it reads the board you started it from;
with two or more it lists them and asks you to close the others.

* **Refill zones** (toolbar, on by default) refills every zone in KiCad and waits for the fill
  to finish before reading, so the zone fills KiPI sees match your current routing. KiPI
  analyzes filled copper only.
* **Load from KiCad** re-reads the board after you edit it, and reloads the saved PI setup
  (save first if you have unsaved changes).
* **Save snapshot… / Open snapshot…** store the extracted board as a file that KiPI can open
  later without KiCad.
* **Mode: DC / AC** (right end of the toolbar) picks the analysis. Sections 2 to 5 are DC,
  the default; [section 6](#6-ac-pdn-impedance) covers AC. The DC setup and results are kept
  while you are in AC mode and come back when you switch.
* **Help** (or **F1**) opens the [user guide](docs/help.html) in your browser.

### 2. Pick the nets

The **Board** panel on the left lists every net. Tick the nets you want to analyze: they are
drawn in 3D and offered in the PI setup pin pickers. **Power & ground only** filters the
list, **All power** ticks every likely power and ground net, and **None** clears it. Type in
the filter box to find a net.

The ticked nets are saved with the PI setup and ticked again the next time the board is
opened. **Load from KiCad** on a board that is already open keeps the current ticks. Only
a board with no saved choice starts with a guess: its largest power planes and the main
ground.

Below that:

* **Copper layers** toggles layers in the view.
* **Display** colors copper by layer or by net, exaggerates the stackup (**Z scale**), and
  shows or hides vias, pads, the board and part **Models**. By layer, each copper layer has
  its color from the PCB editor's color theme (KiCad's default theme if it cannot be read).
* **Models** are the KiCad 3D models of the parts in your setup (VRMs, loads, series links
  and capacitor models) with a pad on a net shown. KiPI has KiCad's `kicad-cli` export them
  from the saved board file, so a part moved since the last save is drawn where it was
  saved. Over results they start hidden; tick **Models** there to show them too. The
  geometry and result views each remember their own setting.
* **Info** shows whatever you last clicked in 3D. **Select in KiCad** selects that pad or
  via in the PCB editor.

The **Stackup** toolbar button opens a window listing every copper and dielectric layer
with its thickness (in µm, mm, mil or oz), each dielectric's material, εr and thermal
conductivity, and each copper layer's conductor and resistivity, with the copper
**Resistivity** and **TCR** for the whole board. The stackup is locked to KiCad's values by
default; untick **Lock to imported values** to try other thicknesses, materials and εr
(choosing a material from the list sets its typical εr and thermal conductivity). Every DC
and AC analysis uses the edited stackup, and the setup keeps it. A copper layer's conductor
(copper foils, silver, gold, aluminum, nickel or tin) sets its resistivity, TCR and thermal
conductivity, and can be changed while the stackup is locked. KiCad's API cannot change a
board's stackup yet, so enter a stackup you settle on in KiCad's Board Setup yourself.

Toolbar buttons set **Top / Bottom / Iso** views and save a **Screenshot…**. Drag to rotate,
scroll to zoom and shift-drag (or middle-drag) to pan. **About** shows the KiPI version and
project links, and **Copy version info** copies the versions of KiPI, Python and its
libraries for a bug report.

### 3. Model the power system

The **PI setup** panel on the right holds the power tree. Each list has **Add**,
**Duplicate** and **Remove** buttons. Hover over any field for a tooltip saying what it does.

As you pick pins, KiPI pre-fills the fields that usually follow from them. Every guess can be
changed, and a field the board gives no answer for is left as it was:

* **VRM set voltage** from the output net's name: `+3V3`, `3.3V`, `V1P8`, `VDD_0V75`, `-5V`,
  `VBUS` (5 V).
* **Ground pins** of a VRM, load or AC probe: the same part's pins on its ground net. Grounds
  aren't always called GND, so KiPI tries grounds the setup already uses, then names like
  `GND`, `PGND` or `VSS`, then the board's biggest net if it isn't named like a rail.
* **VRM input pins** (LDO or switcher): the output part's pins on its other rail, if it has
  exactly one.
* **Series link side B**: side A on a part with pads on just two nets (a resistor, ferrite or
  fuse) puts side B on its other net, and a resistor's value becomes the resistance.
* **New phases** of a multi-phase VRM take the next parts in sequence: `L1` → `L2`,
  `Q1` → `Q2`.
* **Names** follow the picked part while they are still its reference.
Pins are chosen with a picker: pick the net, then the part (type to search), then optionally
the pin numbers (e.g. `1,2,5`; blank means all of that part's pins on the net). Problems such
as missing pins, or two VRMs on one group, are shown in red as you edit.

**VRMs tab**: one entry per regulator or supply.

| Field | Meaning |
|---|---|
| Type | **Source** (ideal supply), **LDO** (I_in = I_out + I_q) or **Switcher** (P_in = P_out / efficiency) |
| Output pins, Set voltage | Where it drives, and to what voltage |
| Input pins | LDO and switcher only. The pins it draws from, which ties this VRM to its upstream net and defines the power tree. |
| Efficiency / Quiescent | Switcher efficiency, LDO quiescent current |
| Output current limit | Optional: the VRM PASSes if its total output current (all phases) is at most this. Leave at **no limit** to skip the check. |
| Dropout | LDO only. The least input-to-output difference it regulates at |
| Input min / max | Optional input range it regulates over. A VRM whose solved input voltage is outside its range, or an LDO below its dropout, cannot hold its set voltage: every load it feeds, directly or downstream, is marked **VRM OUT OF REGULATION** |
| Sense pins | Optional remote sense: the VRM regulates the voltage at these pins instead of at its output |
| Ground pins | Optional: also solve the ground return, so the drop is measured output-to-ground |
| Ground sense pins | Optional negative remote sense on the ground net: the VRM holds its set voltage relative to these pins instead of 0 V (needs ground pins) |
| Phases | Parallel phases of a multi-phase regulator. **Edit phase** picks which phase the output, input and ground pins belong to. |

**Multi-phase VRMs**: set **Phases** above 1, then choose each phase in **Edit phase** and
give it its own output pins (e.g. its inductor), input pins and ground pins. A new phase
takes the next parts in sequence (`L1` → `L2`, `Q1` → `Q2`), or copies the previous one when
there is no next part, so check its pins. All phases share the VRM's set
voltage, type, efficiency and sense pins. Each phase carries an equal share of the output
current and settles at whatever voltage its copper needs to deliver that share. The VRM
regulates its sense pins or, without them, the average of its phase outputs. Each phase draws
its own input current and returns its own ground current. The VRMs results tab lists every
phase under its VRM.

To try it without a design of your own, open the example board in
[`examples/multiphase_buck`](examples/multiphase_buck): a 4-phase 12 V → 0.9 V, 40 A core rail
(`python -m kipi --snapshot examples/multiphase_buck/multiphase_buck.kipi.json.gz`).

**Loads tab**: one entry per consumer.

* **Supply pins** and **Current**. With several supply pins, **Current split** chooses
  between splitting the current evenly over all pins and giving explicit **pin groups**,
  each with its own current (**Add pin group**, or **One group per pin**). Use groups when one
  part draws different currents on different rails or pins, e.g. a core and an I/O supply.
* **Ground pins** (optional) include the ground return.
* **Tolerance** (± %) is the PASS/FAIL limit. Each load is judged by its worst pin: the one
  furthest from the VRM set point, below it (a **drop**) or above it (a **boost**). Boosts
  happen with remote sense, where the VRM raises its output pins to hold a sense point
  further down the copper, so pins nearer the VRM sit above the set point. A load on a net
  group whose solve did not converge shows **NOT CONVERGED** instead of a result.

| Loads tab | Thermal tab |
|---|---|
| ![Load editor](docs/images/setup_loads.png) | ![Thermal settings](docs/images/setup_thermal.png) |

**Series links tab**: parts that pass DC from one net to another, such as sense
resistors, ferrite beads, fuses, load switches and 0 Ω links. Give **Side A pins**,
**Side B pins** and a **Resistance**. Side A on a two-terminal part fills in side B on its
other net. A new link starts on a part that bridges the first two ticked nets that aren't
grounds, if there is one. When both sides are on the same resistor (reference `R…`),
the resistance is read from its value field: `10m`, `0.005`, `4k7`, `0R1`, `5 mΩ` and the like
all work, and anything after the value (`1% 0603`) is ignored. You can still type your own.
Nets joined this way are solved together as one group, and each group may hold only one VRM.
A net with no connection to its VRM is reported, not guessed at. **ESL** is only used by the AC
analysis.

**Solve tab**: every net group, with a checkbox. Untick the groups that are not modeled or
routed yet to solve the rest; only the ticked groups are validated, and the choice is saved.
Where a solved group meets an excluded one, a VRM whose output is excluded becomes a constant
load on its input pins (the input current its rail would need at nominal voltages), a VRM
whose input is excluded becomes an ideal source, and ground pins on an excluded ground are
ignored. The Notes tab lists these substitutions.

![The Solve tab: tick the net groups to solve](docs/images/dc_solve_tab.png)

**Mesh tab**: **Max edge** is the largest triangle edge in mm; smaller is more accurate and
slower. **Via grading** controls how fast the mesh refines towards plated holes, and
**Barrel plating** sets the via wall thickness. The defaults suit most boards.

**Thermal tab** (off by default): tick **Electro-thermal co-simulation** to make losses heat
the board and the warmer copper become more resistive. Set:

* **Ambient** temperature.
* **Iterations**: three is usually enough. The Notes tab shows how much each iteration
  changed.
* **Airflow** in m/s.
* **Grid**, the thermal cell size: *auto* gives about 250 cells across the board.
* **Laminate k**: *from stackup* looks up the in-plane conductivity from each dielectric's
  material in your KiCad stackup (FR4, prepreg types, polyimide, Kapton, PTFE, Rogers,
  ceramic, aluminum…). Unrecognized names fall back to FR4 with a warning. Enter a value
  here to override.
* **Include load power dissipation** (off by default): each load also heats the board with
  the power it draws, the sum over its supply and ground pins of pin current × pin voltage,
  spread over those pins. DC connection (series link) I²R losses always heat their pins.
* **Max copper temperature** and **Max rise above ambient** (optional): copper hotter than
  either limit, on any layer, is marked FAIL in the Thermal results tab. Changing them grades
  the last solve straight away.

The setup is saved as `<board>.kipi-setup.json` next to the board when you click **Save** at
the top of the panel and every time you solve. It travels with the project and can go into
version control.

### 4. Solve

Click **Solve all** (it reads *Solve 3 of 5 net groups* when some are unticked in the Solve
tab). Every net group is meshed and solved, the downstream loads first and then
back towards the source, so each VRM's input current includes everything it feeds. Progress
and solver residuals stream into the log; **Cancel** stops the run.

Tick **HTML report** before solving to also write
`<board>_kipi_report_<date>_<time>.html` next to the board (the time stamp keeps each
solve's report rather than overwriting the last). It opens in your web browser when the solve finishes, and **Report** opens it
again later. It is a single file that works offline, containing:

* the summary and every results table;
* an interactive 3D view per net group (rotate, zoom, hover for values);
* per-layer statistics, with top-down voltage and current-density maps;
* the board temperature map when thermal is on.

| Summary tables | Interactive 3D per net group |
|---|---|
| ![Report summary tables](docs/images/report_tables.png) | ![Report 3D voltage view](docs/images/report_3d.png) |

### 5. Read the results

In the **Results** group, choose a **net group** and a **field** to show in 3D:

* **Voltage**
* **Current density**. Tick **Arrows** to see where and which way the current flows.
* **Power density**
* **Via current**
* **Temperature** (with thermal on)

Voltages are measured against the source's ground pins (0 V). When ground returns are
solved, the ground under a load rises above 0 V, so a supply plane can read above its VRM's set
point even though a remotely sensed rail is exactly on target. The Loads tab's effective
voltage (supply minus ground at the load) is the figure to compare with the set point.

The color scale covers the copper layers that are shown: untick a layer under **Copper
layers** and the colors stretch over the layers that remain, so one layer's outlier does not
flatten the rest. (Current and power density clip at the 99.5th percentile, since pad corners
spike; the probe still reports true values.)

Click any point on the copper or any via to probe it: the Info box gives the voltage, its drop
or boost from the set point, current and power density, and the pad or via hit. **Show
copper** returns to the plain geometry view; pick the group again, or another field, to get
the results back.

![Voltage across the +3.3V plane, with the Loads results table](docs/images/voltage.png)

![Current density with flow arrows on the 5 A +12Vin input path](docs/images/current_density.png)

| Via current | Temperature |
|---|---|
| ![Via current, with the Vias table ranked by current](docs/images/via_current.png) | ![Board temperature from the electro-thermal solve, with the Notes tab](docs/images/temperature.png) |

Result tabs (click a column header to sort by it; click again to reverse):

| Tab | Shows |
|---|---|
| Loads | Each load's current, effective voltage, drop or boost, worst pin and PASS/FAIL. Double-click to show its group. |
| VRMs | Output and input voltage, current and power, conversion loss, and PASS/FAIL against the output current limit, with a row per phase for multi-phase VRMs |
| Series links | Current and loss in each series part |
| Vias | Every via and PTH barrel ranked by current. Set a **Current limit** for PASS/FAIL, and double-click a row to fly to it. |
| Losses | Copper loss per group and layer, vias, series links and VRM conversion |
| Thermal | After an electro-thermal solve: the hottest copper on each layer, its rise and location, PASS/FAIL against the temperature limits, the iteration history and the dielectric conductivities used. Double-click a row to show that group's temperature. |
| Notes | Solver details, warnings and the thermal iteration history |

**Export ▾** writes the tables as CSV (including `_pins.csv`, with the voltage and current
of every pin each group was solved with, which the HTML report also lists), the 3D
results as a ParaView `.vtm`, or **Save
results…** as a `.kipi-results` file (named with the board and the time by default), which the toolbar's **Open results…** reloads without
solving again (load the board first). Results files are Python pickles, so only open ones
you created.

### 6. AC: PDN impedance

Switch **Mode** to **AC**. The setup panel becomes the **AC setup**, which shares the VRMs and
series links with DC (fields only DC uses are grayed out) and adds:

* **Capacitors tab**: RLC models (capacitance, ESL, ESR) for the decoupling capacitors, each
  shared by a group of identical parts. **Models from values** makes one model per capacitor
  value that has none yet, reading the capacitance from the value; enter the ESL and ESR
  from the datasheet. The table lists every capacitor between ticked nets: tick parts to
  assign them to the selected model, with **Tick same value**, **Tick shown** and
  **Untick shown** for many at once. Enter only the part's own ESL: its mounting (pads,
  vias, planes) is simulated.
* **Probes tab**: where the impedance is looked into, usually a device's supply and ground
  pins. Each probe has a target: **flat** (an impedance up to a frequency) or **custom**, a
  piecewise-linear mask through frequency/impedance points on the log-log plot.
  **Target Impedance/Frequency Calculator…** works out a flat target from the device's AC
  voltage tolerance, load step and slew rate: Z = V / ΔI, up to f = 1 / (π · t_rise).
  Targets apply only above the rail's VRM **Control loop bandwidth**: the AC solve leaves
  the VRM out of circuit (the open-loop PDN impedance), and below its bandwidth the
  converter's loop holds the rail. If the bandwidth is at or above the target frequency,
  the whole target is judged and the results say so. The method follows Smith et al.,
  ["Power Distribution System Design Methodology and Capacitor Selection for Modern CMOS
  Technology"](https://doi.org/10.1109/6040.784476), IEEE Trans. Adv. Packaging, 1999.
* **Sweep tab**: start and stop frequency, points per decade, and how many copper field
  solves per decade (the copper's response is interpolated between them).
* **Mesh tab**: mesh size, minimum edge at plated holes (which sets the via inductance),
  dielectric loss tangent and default εr.

| Capacitor models | Target calculator |
|---|---|
| ![Capacitors tab](docs/images/ac_capacitors.png) | ![Target impedance calculator](docs/images/ac_target_calc.png) |

Pick the **Rail** (by its VRM) and **Ground** under the tabs and click **Solve rail**. The copper
of the rail and its ground is field-solved as a 2.5D model with both faces of every sheet:
plane resistance with skin effect, spreading inductance, plane-pair capacitance and cavity
resonances, and via inductance all come out of the mesh. The capacitors and series links are
then attached at every sweep frequency. The VRM itself is not modeled as an impedance, so the
curve keeps rising towards low frequency as the bulk capacitance runs out.

The result opens in the **Impedance** tab next to the 3D view: |Z| against frequency for every
probe, with its target dashed in the same color. Click a curve to place a cursor; drag it or
use ←/→ (Shift: ×10) to move along the curve, ↑/↓ to change curve, and Esc or right-click to
remove it. The **Board map** tab beside it shows the solve from above while it runs and
after: the mesh of the rail and its ground, layer by layer, with the probes and the
capacitors marked by model. The Info box shows the cursor's frequency, |Z|, phase, R and X, the reactance as an
equivalent L or C, PASS/FAIL against the target with the margin in dB, and every other
probe's |Z| at that frequency. The results table gives each probe's minimum and worst |Z|
(closest to, or furthest over, the target) and PASS/FAIL; the notes list the capacitors that
were left out for having no model. Each rail keeps its last result, so switching **Rail** shows
another rail's curves without solving again. **Export CSV…** writes |Z|, R and X at every
frequency. **Save results…** writes every solved rail to a `.kipi-ac-results` file, which
**Open results…** reloads in AC mode (load the board first; like DC results files, these are
pickles, so only open ones you created).

Tick **HTML report** next to **Solve rail** to also write
`<board>_<rail>_ac_report_<date>_<time>.html` next to the board after each solve. It opens in your web browser when the solve
finishes, and **Report** opens it again later. Like the DC report, it is one
offline file you can share. It has the probe table with margins, an interactive |Z|
plot with the targets and each capacitor model's own curve, a board map of the rail's copper
with the probes and capacitors marked, the capacitor models, and the sweep, mesh and solver
details.

![The impedance plot of the example's 0.9 V core rail against its custom target, with the cursor on the anti-resonance and its values in the Info box](docs/images/ac_impedance.png)

### Tips and troubleshooting

* **"No copper path" between a VRM and a load** usually means unrouted copper, an unfilled
  zone, or a missing series link for a part between them.
* **Unexpected drop:** compare the board view with KiCad. Thermal reliefs, necked traces and
  via counts dominate DC drop, and the Current density view with Arrows makes the bottleneck
  obvious.
* **Slow solves:** raise **Max edge**, or analyze fewer nets at a time. Large planes are cheap
  but very fine traces are not.
* **"KiPI works with one KiCad at a time"** although only one is open: a KiCad that did
  not exit cleanly may still be running; end it in the task manager (the message gives its
  PID). If the check is wrong for your setup, set the environment variable
  `KIPI_ALLOW_MULTIPLE_KICAD=1` before starting KiCad to skip it.
* **No part models:** the log says why. KiPI needs KiCad's `kicad-cli` (it looks in the
  connected KiCad, the PATH and the usual install folders; set `KIPI_KICAD_CLI` to its
  path otherwise) and the board's `.kicad_pcb` file on disk. A part whose model file KiCad
  cannot find is named with the missing path; KiCad's own 3D viewer shows the same gap.
* **Logs:** each session writes to `kipi/kipi-<date>-<time>-<pid>.log` in the temporary
  folder (`%TEMP%` on Windows, `$TMPDIR` on macOS, `/tmp` on Linux), and the last
  15 sessions are kept. A matching `.crash.log` collects native crash tracebacks and, if a
  task runs longer than a minute, a stack dump of every thread. If the window stops
  responding during a task, the log records where the UI thread was stuck. Attach both
  files, and the **About** dialog's version info, to bug reports.

## Development

KiPI also runs outside KiCad, against the running PCB editor or a saved snapshot:

```powershell
python -m venv --system-site-packages .venv   # use KiCad's bundled python.exe
.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python -m kipi                         # connect to the running PCB editor
.venv\Scripts\python -m kipi --dump board.kipi.json.gz   # extract only
.venv\Scripts\python -m kipi --snapshot tests\data\synthetic.kipi.json
.venv\Scripts\python -m pytest tests
.venv\Scripts\ruff check .                    # lint, as CI does (ruff check . --fix fixes most)
```

`requirements.txt` is what KiCad installs into the plugin's venv; test and lint tools live
only in `requirements-dev.txt`.

The tests follow the package: `tests/core` (geometry, meshing, solvers, KiCad I/O),
`tests/dc` (power tree, thermal, DC reports and panel), `tests/ac` (impedance solve, targets,
AC panel and report), `tests/gui` (main window and app) and `tests/scripts` (CI scripts).
Shared test boards and the fake KiCad live in `tests/helpers`, which pytest puts on the
import path; each folder also runs on its own (`pytest tests/dc`). CI runs each folder in
its own log group.

CI runs ruff and the test suite for every push and pull request: on Windows with Python 3.11,
on Linux with Python 3.10 to 3.13, on macOS, and with the oldest kicad-python that
`requirements.txt` allows. A further job checks whether every dependency has prebuilt
packages for Python 3.14; it is expected to fail until Triangle and PyAMG publish them.

Two more workflows feed the About dialog's statistics. `release.yml` attaches
`KiPI-<tag>.zip` to each published release (run it by hand with a tag for an older
release); GitHub counts downloads of uploaded assets only. `stats.yml` runs daily and
writes `stats.json` to the `stats` branch, keeping the clone history beyond the 14 days
GitHub remembers. Clone counts need a `TRAFFIC_TOKEN` repository secret: a fine-grained
token for this repo only, with *Administration: read-only*. Without it the workflow still
updates the download counts and warns.

`tests/helpers/kicad_fake.py` stands in for a running KiCad. It answers the IPC API's own
messages the way each KiCad version from 9.0.0 to 10.0.6 does, refusing commands that
version did not have yet, and `tests/core/test_kicad_versions.py` extracts a board from each one.
When a KiCad release adds a command KiPI starts using, add it to `ADDED` there.

The tests also solve the example board and check that its committed setup still matches
`make_board.py`. After changing the generator, rerun
`python examples/multiphase_buck/make_board.py` and commit both output files.

### Layout

| Path | Purpose |
|---|---|
| `kipi_entry.py`, `plugin.json` | KiCad IPC plugin action |
| `kipi/core/` | Shared by every analysis; no Qt and no analysis-specific physics: |
| `kipi/core/kicad_io/` | kipy → `BoardSnapshot` extraction, JSON snapshot save/load |
| `kipi/core/model/` | Toolkit-independent board model, power-net heuristics |
| `kipi/core/geometry/` | Per-net, per-layer copper union (shapely) |
| `kipi/core/mesh/` | Conforming per-layer triangle mesh (Triangle), graded around plated holes |
| `kipi/core/solver/` | Sparse linear algebra: direct backends (PARDISO / SuperLU), direct or AMG-CG SPD solver |
| `kipi/core/viz/` | PyVista copper meshes and batching (no Qt) |
| `kipi/core/progress.py` | Progress/log reporting used by every pipeline stage |
| `kipi/dc/` | DC analysis: |
| `kipi/dc/solver/` | 2.5D DC conduction FEM: sheet conductance, via/PTH barrels, series parts, sources, loads |
| `kipi/dc/pi/` | User power-tree setup (VRMs, loads, series links), JSON, validation, the guesses that pre-fill it, tree solver, results tables |
| `kipi/dc/thermal/` | Board heat model, stackup material conductivities, electro-thermal iteration |
| `kipi/dc/report/` | Self-contained HTML report (Plotly 3D, matplotlib maps) |
| `kipi/dc/viz/` | DC result meshes: voltage, drop, current density, power (no Qt) |
| `kipi/dc/gui/` | The PI setup and results panel |
| `kipi/ac/` | AC analysis: PDN impedance over frequency. Capacitor and probe model, target impedance, the 2.5D two-face field solver and sweep (`solver/`), the HTML report (`html_report.py`), the board map data (`board_map.py`), the AC setup panel, impedance plot and Board map tab (`gui/`) |
| `kipi/gui/` | PySide6 application shell shared by both modes: main window, mode switch, 3D viewer, worker threads, About and Help |
| `docs/help.html` | The user guide opened by **Help**; a single offline HTML file |
| `kipi/compat.py` | Opens results files saved by earlier versions, from before modules moved |
| `examples/` | Example boards as snapshots with their PI setups, and the scripts that generate them |

## License

KiPI is free software: you can redistribute it and/or modify it under the terms of the
[GNU General Public License](LICENSE) as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version. It is distributed WITHOUT
ANY WARRANTY; see the license for details. Copyright © 2026 Drew Maatman.

**Note on the mesher.** KiPI meshes copper with Jonathan Shewchuk's
[Triangle](https://www.cs.cmu.edu/~quake/triangle.html), through the
[`triangle`](https://pypi.org/project/triangle/) Python package. Triangle is not open source
in the OSI sense. Its author's terms allow free use and redistribution, but it **may not be
sold or included in commercial products without a license from its author**. Using KiPI for
your own design work is fine. If you want to ship KiPI, or anything built on it, as part of
a commercial product, you will need to arrange a license for Triangle with its author or
swap in another mesher. The meshing code is isolated in `kipi/core/mesh/` for that reason.

The other dependencies are under permissive licenses, apart from PySide6 (LGPL-3.0). All of
them are compatible with the GPL.
