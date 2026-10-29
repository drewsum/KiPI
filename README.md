<p align="center">
  <img src="resources/kipi.gif" alt="KiPI logo" width="128">
</p>

# KiPI — DC, AC and Coupled Noise Power Integrity Simulator (Standalone or KiCad Plugin)

[![CI](https://github.com/drewsum/KiPI/actions/workflows/ci.yml/badge.svg)](https://github.com/drewsum/KiPI/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![KiCad 9.0+](https://img.shields.io/badge/KiCad-9.0%2B-314cb0.svg?logo=kicad&logoColor=white)](#requirements)
[![Platform: Windows | Linux | macOS](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)](#requirements)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10%E2%80%933.13-3776ab.svg?logo=python&logoColor=white)](#requirements)

**New to power integrity? Read the [Solver Guide](https://drewsum.github.io/KiPI/solver-guide.html)**
([source](docs/solver-guide.html)): what PI analysis is, why it matters, and how KiPI's mesh
and its DC, thermal, AC and coupled noise solvers work.

KiPI is a power integrity analyzer that runs standalone or as a KiCad 9 and 10 IPC plugin. It has three
analysis modes:

* **DC: Voltage Drop.** Define voltage regulators (VRMs), loads (current into pins) and series
  parts (beads, resistors, load switches), then simulate the voltage drop net by net, across
  every copper layer. Inspect voltage, current density, via currents, copper power
  dissipation and (optionally) board temperature in a rotatable 3D view, with PASS/FAIL
  checks for loads, VRMs, vias and temperature, and write an interactive HTML report or a
  PDF.
* **AC: PDN Impedance.** Give the decoupling capacitors RLC models and put probes on a
  device's power pins, then sweep the impedance of the power distribution network (planes,
  vias, mounting and capacitors) over frequency. Check it against a flat or custom target
  impedance, with a calculator that determines the target from the load's transient spec.
* **Coupled Noise: Switcher Noise on Signal Nets.** Treat a buck or boost regulator as an
  aggressor: KiPI builds its switch node, inductor and capacitor ripple waveforms from the
  layout, then works out the noise they couple onto the signal nets you name (the victims)
  through capacitance, mutual inductance and shared ground. Each victim's receiver is
  checked against its logic family's noise margin, with the line held high and held low, and
  the 3D view shows where along its traces the noise couples in.

New to KiPI? The **[user guide](docs/help.html)** (also the **Help** button in KiPI, or
**F1**) walks through every button, the workflows and what the results mean. Every
control in KiPI has a tooltip. This README is an overview; the user guide has the details.

![Current density with flow arrows through the +12V input path and the U201 eFuse, with the Series links table](docs/images/current_density.png)

**Status: 0.9.4 (alpha, in development)** ([changelog](docs/CHANGELOG.md)). KiPI runs on its own too, with an app launcher that handles dependencies, and imports boards from other layout tools. The author wants to add support for more PCB file formats. If you have an example with good complexity which uses a wide variety of your PCB design tool's features that you are willing to share, please open an [issue](https://github.com/drewsum/KiPI/issues) and attach your PCB file there.

## Install

KiPI runs two ways, from the same folder:

* **As a KiCad plugin**, the main way: click the **KiPI** button in the PCB editor and KiPI
  reads the board open there. For this, the KiPI folder **must be in KiCad's plugins
  folder**, and KiCad installs its dependencies.
* **Standalone**: KiPI started on its own, with its launcher, opens board files and needs
  no KiCad. The folder can be **anywhere**; the launcher installs the dependencies on the
  first start. It opens KiCad 7 to 10 board files (`.kicad_pcb`), ODB++ jobs, HyperLynx
  files (`.hyp`) and KiPI snapshots (`.kipi.json.gz`). Reading CCE files is on the way
  ([#143](https://github.com/drewsum/KiPI/issues/143)).

### As a KiCad plugin

#### Requirements

* **KiCad 9.0 or later.** Developed on KiCad 10 and confirmed on KiCad 9. Before 9.0.3,
  KiCad cannot say which layers a plated pad has copper on, so pads are kept on every layer
  (KiPI warns in its log), and 9.0.0 cannot report net classes.
* **Python 3.10 to 3.13**, the one KiCad runs plugins with (some dependencies have no
  prebuilt packages for Python 3.14 yet).
  * **Windows:** KiCad's own Python 3.11. Nothing to do.
  * **Linux:** KiCad uses the system Python. On a distribution with Python 3.14 (Ubuntu
    26.04, Fedora 43), pip has to compile some packages, which needs
    `sudo apt install build-essential python3-dev` or similar.
  * **macOS:** KiCad bundles Python 3.9, which is too old. Install Python 3.12 (for
    example from python.org or Homebrew) and select it as the **Python interpreter** in
    *Preferences → Plugins*.

#### Steps

1. In KiCad, enable **Preferences → Plugins → Enable KiCad API**.
2. Clone, link (or copy) this repo into your KiCad plugins folder, or uncompress the release source zip. The examples use KiCad 10's
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

### Standalone

1. Install **Python 3.10 to 3.13** if you do not have one (from
   [python.org](https://www.python.org/downloads/), or your package manager). On Linux, add
   `python3-venv` (Debian, Ubuntu) and, for the setup window, `python3-tk`.
2. Put the KiPI folder anywhere: clone this repo, or unzip `KiPI-<version>.zip` from the
   [latest release](https://github.com/drewsum/KiPI/releases/latest).
3. Start KiPI with its launcher: double-click **`kipi_launcher.pyw`** (Windows), or run

   ```sh
   python kipi_launcher.py                      # or: python kipi_launcher.py <board>.kipi.json.gz
   ```

   On Windows the first start leaves a **KiPI** shortcut next to it. On Linux and macOS, run
   `python3 kipi_launcher.py --create-shortcut` once to add KiPI to the applications menu
   (Linux) or `~/Applications` (macOS).

The first start sets up KiPI. The setup window lists every package KiPI will install with
its size (about 254 MB to download on Windows), and nothing is installed until you click
**Install**. On Windows and Linux x86-64 it also offers
[PARDISO](#optional-faster-solves-on-large-boards-windows-and-linux-x86-64). Everything goes
into an environment of KiPI's own, outside the KiPI folder (`KIPI_HOME` puts it elsewhere):

| | KiPI's environment |
|---|---|
| Windows | `%LOCALAPPDATA%\KiPI` |
| macOS | `~/Library/Application Support/KiPI` |
| Linux | `~/.local/share/kipi` |

![The launcher's setup window on a first start: every package with its size, and the optional PARDISO solver](docs/images/launcher_setup.png)

Use **Open…** to open a board and **Reload** to read it again. KiCad boards keep the same
setup file standalone and in the plugin. KiPI uses the zone fills saved in the file, so
**fill the zones in KiCad (B) and save** before opening a `.kicad_pcb`. ODB++ jobs and
HyperLynx files share a board's setup when exported next to it with the same name
(`Board.zip`, `Board.hyp`). The [user guide](docs/help.html) lists what each format does
and does not carry.

To change the setup later, use **About → Manage dependencies…** in KiPI, or the launcher:

```sh
python kipi_launcher.py --setup              # the setup window again (add or remove PARDISO)
python kipi_launcher.py --add-pardiso        # or --remove-pardiso, without questions
python kipi_launcher.py --repair             # reinstall everything from scratch
python kipi_launcher.py --reset              # delete KiPI's environment
python kipi_launcher.py --create-shortcut    # add KiPI to the Start menu / app menu / ~/Applications
```

If setup fails, the launcher says why; everything the installer printed is in the `logs`
folder next to the environment.

### Optional: faster solves on large boards (Windows and Linux, x86-64)

If the optional [`pypardiso`](https://pypi.org/project/pypardiso/) package is installed,
KiPI solves with Intel MKL's multithreaded PARDISO solver in place of SciPy's
single-threaded one. On a 645k-unknown rail one solve drops from about 8 s to 1.2 s. Small
boards gain little. It costs about 210 MB of download on Windows (320 MB on Linux) and
there are no MKL packages for macOS or ARM.

KiPI installs it by default where it can, with an opt-out: in the launcher's first setup
standalone, or in a prompt the first time the plugin opens. *About → Manage dependencies…*
adds or removes it later, and *About → Copy version info* shows which solver is in use. If
PARDISO cannot load, KiPI falls back to SciPy and says so in the log.

## Using KiPI

### 1. Open your board in KiCad

Open your board in the PCB editor and click the **KiPI** toolbar button. KiPI opens in its
own window and reads the board through the KiCad API; the log at the bottom streams each
stage as it runs.

* **Load from KiCad** re-reads the board after you edit it in KiCad, and reloads the saved PI setup.
* **Refill zones** (on by default) refills every zone in KiCad before reading, so the zone
  fills KiPI sees match your current routing. KiPI analyzes filled copper only.
* **Save snapshot… / Open snapshot…** store the extracted board as a file that KiPI can open
  later without KiCad. See example snapshots in the repo to experiment!
* **Mode: DC / AC / Noise** (right end of the toolbar, or **Ctrl+1** / **Ctrl+2** / **Ctrl+3**)
  picks the analysis mode. Readme sections 2 to 5 are DC, the default;
  [section 6](#6-ac-pdn-impedance) covers AC and [section 7](#7-coupled-noise) Coupled Noise.
  The VRMs, series links and capacitor models are shared by all three.
* **Help** (or **F1**) opens the [user guide](docs/help.html) in your browser.

### 2. Pick Power Nets from Netlist

The **Board** panel on the left lists every net in the extracted design. Tick the nets you
want to analyze: only ticked nets can be simulated, and they are offered in the PI setup pin
pickers. **Power & ground only**, **All power** and **None** help pick them, and the ticks
are saved with the setup.

Below that are the **Copper layers**, the **Display** options (color by layer or net, Z
scale, vias, pads, mask, silkscreen, part **3D Models**), and **PI Models**, which marks the
parts of the current analysis in 3D. Clicking a part in 3D selects it in the setup, and the
**Info** box shows whatever you last clicked, with **Select in KiCad** to find it in the PCB
editor.

![PI Models marking the parts of the analysis, snapped to the selected +3.3V switcher](docs/images/pi_models.png)

The **Power tree** toolbar button opens a block diagram of the modelled power network, left
to right in the direction power flows, and can be saved as a PNG.

![The power tree of the multiphase buck example: a 12 V source, the 4-phase switcher, the 0.9 V core rail, its load and capacitor models](docs/images/power_tree.png)

The **Stackup** toolbar button lists every copper and dielectric layer with its thickness,
material, εr, thermal conductivity and resistivity, with a cross-section drawn to scale.
Untick **Lock to imported values** to try other thicknesses and materials: every analysis
uses the edited stackup.

![The Stackup window: copper and dielectric layers with thickness, material, εr, thermal conductivity and resistivity](docs/images/stackup.png)

Drag to rotate, scroll to zoom and shift-drag (or middle-drag) to pan; the
[user guide](docs/help.html#keys) lists every keyboard shortcut.

### 3. Model the Power Distribution System

The **PI setup** panel on the right holds the power tree. Each list has **Add**,
**Duplicate** and **Remove** buttons, and every field has a tooltip. Pins are chosen with a
picker: the net, then the part, then optionally the pin numbers. As you pick pins, KiPI
pre-fills what usually follows from them (a VRM's target voltage from the net name, ground
pins, a series link's other side and a resistor's value), and problems are shown in red.
**Import…** fills the power tree from a Tomachie power-tree file (`<design>.pdn.json`).
It also reads a HyperLynx BoardSim session file (`.bud`): its power nets are ticked, and its
`DC_CONN` series links, decoupling capacitor groups, VRMs (as ideal sources, with their
remote sense) and DC sinks (as loads) are added to the setup.

**VRMs tab**: one entry per regulator, power input, or ideal supply. A VRM is a **Source**,
**LDO**, **Switcher** or **Isolated** converter, with its output pins and target voltage,
its input pins (which link it to the rail upstream), efficiency or quiescent current, and
optional remote sense, ground pins, load line, dropout, input range, DC accuracy, current
limit and multiple **Phases**. KiPI works out each VRM's input current from what it feeds.

To try it without a design of your own, open the example board in
[`examples/multiphase_buck`](examples/multiphase_buck): a 4-phase 12 V → 0.9 V, 40 A core rail
(`python -m kipi --snapshot examples/multiphase_buck/multiphase_buck.kipi.json.gz`).

**Loads tab**: one entry per consumer: its **Supply pins**, **Current** (split evenly or by
pin group), optional **Ground pins** and a **Tolerance** for PASS/FAIL. **Load models**
(**Model ▾**) save and apply a part's supplies as a YAML file. They also read a
HyperLynx PI model (`.hlpimodel`): its DC sink currents, pin by pin, as a load on each net
the pins are on, on as many parts as you tick.

![Load editor](docs/images/setup_loads.png)

**Series links tab**: parts that pass DC from one net to another, such as sense
resistors, ferrite beads, fuses, load switches and 0 Ω links: **Side A pins**, **Side B
pins** and a **Resistance**. **Import SPICE model…** uses the maker's SPICE model of the part
for both DC and AC, including diodes and multi-pin parts such as common-mode chokes.
**In order to assign a series link, the nets on either side must both be defined as power
nets in the net selection pane.**

**Solve tab**: every combined rail, with a checkbox. Untick the rails that are not modeled or
routed yet to solve the rest.

![The Solve tab: tick the net groups to solve](docs/images/dc_solve_tab.png)

**Mesh tab**: **Max edge** is the largest FEA mesh triangle edge in mm; smaller is more
accurate and slower. **Via plating** sets each kind of via's plating thickness and fill.

![The Mesh tab with the via plating rules](docs/images/setup_mesh.png)

**Thermal tab** (off by default): tick **Electro-thermal co-simulation** to make losses heat
the board and the warmer copper become more resistive. Each copper layer has its own
temperature, with heat crossing the laminate and running down vias. Inspired by
[KiCad Thermal Sim](https://github.com/PoeAlex/KiCad_Thermal_Sim) by
[PoeAlex](https://github.com/PoeAlex).

![Thermal settings](docs/images/setup_thermal.png)

The setup is saved as `<board>.kipi-setup.yml` next to the board when you click **Save** and
every time you solve. It travels with the project and can go into version control. AI agents
can make quick work of power tree modelling by editing this file directly.

### 4. Solve

Click **Solve all**. Every rail is meshed and solved, downstream loads first, so each VRM's
input current includes everything it feeds. Progress streams into the log; **Cancel** stops
the run.

Click **Report** to write an HTML report next to the board, or choose **PDF**. The HTML
report is a single portable file with the summary and every results table, an interactive 3D
view per rail, per-layer voltage and current-density maps, the board temperature map when
thermal is on, and a **User notes** box.

| Summary tables | Interactive 3D per net group |
|---|---|
| ![Report summary tables](docs/images/report_tables.png) | ![Report 3D voltage view](docs/images/report_3d.png) |

### 5. Read the results

In the **Results** pane, choose a **net group** and a **field** to show in 3D: **Voltage**,
**Current density** (tick **Arrows** to see the flow), **Power density**, **Via current**
or **Temperature**. Click any point to probe it in the Info box, and set the **Color
scale** to look closer.

![Voltage drop along the +12V input plane, from the input connector to the two loads, with the Loads results table](docs/images/voltage.png)

![Current density with flow arrows through the +12V input path and the U201 eFuse, with the Series links table](docs/images/current_density.png)

| Via current | Temperature |
|---|---|
| ![Via current, with the Vias table ranked by current](docs/images/via_current.png) | ![Board temperature from the electro-thermal solve, with the Thermal results tab](docs/images/temperature.png) |

The result tabs give **Loads** (effective voltage, drop and PASS/FAIL), **VRMs**, **Series
links** (I²R losses), **Vias** ranked by current, **Losses**, **Thermal** and **Notes**.
**Export ▾** writes CSV or ParaView files, and **Save results…** writes a results file that
**Open results…** reloads without solving again.

### 6. AC: PDN impedance

Switch **Mode** to **AC**. The **AC setup** shares the VRMs and series links with DC mode and
adds:

* **Capacitors tab**: RLC models (capacitance, ESL, ESR) for the decoupling capacitors, each
  shared by a group of identical parts, or the maker's SPICE model. **Models from values**
  starts one model per capacitor value, and **Import…** / **Export…** move a model between
  setups as a YAML file named after it. Models can be derated for DC bias, temperature,
  aging and tolerance. **Important**: model capacitors of the same value in different case
  sizes separately.
* **Probes tab**: where to plot the PDN impedance, usually a device's supply and ground pins,
  each with a **flat** or **custom** target. The **Target Impedance/Frequency Calculator**
  works out a flat target from the device's voltage tolerance and load step, following Smith
  et al., ["Power Distribution System Design Methodology and Capacitor Selection for Modern
  CMOS Technology"](https://doi.org/10.1109/6040.784476), IEEE Trans. Adv. Packaging, 1999.
  Targets apply above the VRM's **Control loop bandwidth**.
* **Sweep tab**: frequency range and resolution, and the capacitor deratings.
* **Mesh tab**: mesh size and minimum edge at plated holes.

| Capacitor models | Target calculator |
|---|---|
| ![Capacitors tab](docs/images/ac_capacitors.png) | ![Target impedance calculator](docs/images/ac_target_calc.png) |

Pick the **Rail** and **Ground** and click **Solve rail**. The rail's copper and its ground
are field-solved, so plane resistance, spreading inductance, plane capacitance, cavity
resonances and via inductance all come from the layout; the capacitors and series links are
attached at every frequency. The VRM is not modeled as an impedance (the open-loop PDN).
**Important:** AC analysis is a massive compute problem. Many probes and a finer mesh
increase simulation time dramatically.

The **Impedance** tab plots |Z| against frequency for every probe, with its target, and a
cursor to read values. The **Board map** tab shows the mesh, probes and capacitors, and the
**Transient** tab estimates each probe's undershoot and overshoot for its load step.
**Report**, **Export CSV…** and **Save results…** work as in DC mode.

![The Board map of the core rail's solve: the VCORE and GND mesh, the probe and the capacitors by model](docs/images/ac_board_map.png)

![The impedance plot of the example's 0.9 V core rail against its custom target, with the cursor on the anti-resonance and its values in the Info box](docs/images/ac_impedance.png)

### 7. Coupled Noise

Switch **Mode** to **Noise**. The **Coupled Noise setup** treats one of the setup's
**Switcher** VRMs as an *aggressor* and the signal nets you name as *victims*. The
screenshots below are the `examples/validation/coupled_noise` example: a 12 V to 1.8 V,
1 MHz buck over a ground plane, with three victims. Open its snapshot to try it.

* **Aggressors tab**: **Add** makes an aggressor from a switcher VRM and fills it in from the
  board: the switching device, inductor, switch node and capacitors. Give the switching
  frequency and edge times; datasheet values such as Coss and Qrr are optional. KiPI traces
  each capacitor's hot loop through the copper for its inductance and its coupling.
* **Victims tab**: each victim is a net with its driver and receiver pins and a **Type**
  (CMOS, open drain, terminated, LVDS, analog input or undriven) and **Logic level**, which
  set its loading and noise margin.
* **Capacitors tab**: the same capacitor models as AC mode.
* **Settings tab**: harmonics, damping, victim resolution and the **Board model**, the slow
  but more accurate field model of the converter's copper (untick it for a quick look).

Pick the **Aggressor** and click **Solve**. Tick **Scan nearby nets** to also check every
signal net near the aggressor. The **Waveforms** tab plots the switch node with its ringing,
the inductor and capacitor currents and the rail ripple, in time or as a spectrum.

![The Waveforms tab: the buck's switch node over two periods, ringing at 179 MHz on each hard rising edge while the soft falling edge settles at once](docs/images/noise_waveforms.png)

The **Results** table lists each victim's peak noise at its receiver, its margin, the
dominant mechanism and PASS or FAIL. **Victim noise** plots the noise, as a whole or by
mechanism (capacitive, inductive, common ground).

![Victim noise for SIG_SW: the superposition at the receiver with its capacitive and inductive parts, and the results table](docs/images/noise_victim.png)

**Victim held high and low** draws the noise riding on the receiver's high and low levels
against its thresholds.

![SIG_SW held high and low, with the noise riding on each level](docs/images/noise_states.png)

**Coupling in 3D** colors the selected victim's traces by their share of the peak noise, so
the pieces to move stand out. **Hot loops in 3D** draws the capacitor loops' current, and
**Fields in 3D** the H and E fields over the board, with near-field hotspots and a first
radiated estimate against CISPR 32 class B. Victims crossing a split in their reference
plane are noted.

![Coupling in 3D for SIG_LOOP: the victim colored by its share of the 631 mV peak, beside the capacitor loops](docs/images/noise_coupling_3d.png)

The mechanisms point to different fixes: capacitive noise comes from the switch node's
dV/dt, inductive noise from the hot loop's dI/dt and common noise from the switching
current's return. **Report** and **Save results…** work as in the other modes.

### Tips and troubleshooting

* **A load shows DISCONNECTED** usually means unrouted copper, an unfilled zone, or a
  missing series link for a part between two power nets.
* **Unexpected voltage drop:** thermal reliefs, necked traces, via counts and anti-pads in
  planes dominate DC drop; the Current density view with Arrows shows the bottleneck.
* **Slow solves:** raise **Max edge**, or analyze fewer nets at a time. On Intel x86, install
  the PARDISO solver (above).
* **"KiPI works with one KiCad at a time"** although only one is open: a KiCad that did not
  exit cleanly may still be running; end it in the task manager.
* **Logs:** each session writes to `kipi/kipi-<date>-<time>-<pid>.log` in the temporary
  folder, with a matching `.crash.log`. Attach both, and **About → Copy version info**, to
  bug reports. The [user guide](docs/help.html) has more troubleshooting.
* **Have Fun:** The author asks that you share what projects this package has simulated, and what power integrity issues you have found with it. Conductor simulation gives incredible insight into how PCBs actually function in practice. See what happens when you pump 10 amps into a 5 mil trace with the temperature results view.

## Development

From a checkout, KiPI runs standalone, or as the plugin against the running PCB editor:

```powershell
python -m venv --system-site-packages .venv   # use KiCad's bundled python.exe
.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python -m kipi tests\data\synthetic.kipi.json   # standalone, opening a board
$env:KIPI_HOST = "plugin"; .venv\Scripts\python -m kipi        # the plugin: connect to KiCad
.venv\Scripts\python -m kipi --dump board.kipi.json.gz   # extract from KiCad only, no window
.venv\Scripts\python -m pytest tests
.venv\Scripts\ruff check .                    # lint, as CI does (ruff check . --fix fixes most)
```

`requirements.txt` is what KiCad installs into the plugin's venv, and
`requirements-standalone.txt` what the launcher installs; `tests/core/test_requirements.py`
keeps them in step with `pyproject.toml`. After changing them, run
`python .github/scripts/dep_sizes.py`.

The tests follow the package (`tests/core`, `tests/dc`, `tests/ac`, `tests/noise`,
`tests/sources`, `tests/gui`, `tests/scripts`). `tests/accuracy` solves the boards in
[`examples/validation`](examples/validation) and compares the results with hand
calculations, to help gain confidence that an open-source tool can compare to professional
tools:

| Board | Checks |
|---|---|
| `skinny_traces` | 0.15 mm and 0.1 mm tracks on outer and inner layers (ρL/wt), an arc track (annular sector) |
| `right_angle` | an L route and a Z route: 0.5587 squares per right-angle corner |
| `layer_change` | a strip dropping from F.Cu to In2.Cu through a row of vias: barrels in parallel, current crowding, equal sharing |
| `planes` | two pads on a disc-shaped inner plane (method of images); one strip on two layers of different weights |
| `power_chain` | source, switcher, ferrite series link and remote-sensed LDO over a ground strip: every voltage and current |
| `ac_plane` | plane-pair capacitance ε₀εrA/h and the first cavity resonance c/(2a√εr) |
| `ac_decap` | a mounted capacitor as a series RLC: low-frequency capacitance, mounted inductance, ESR at resonance |
| `ac_ferrite` | a ferrite (R + L link) feeding a small plane: lumped impedance and its LC resonance |

After changing a board there, rerun `python examples/validation/make_boards.py [name]` (or
`python examples/multiphase_buck/make_board.py` for the example) and commit its files.

CI runs ruff and the test suite for every push and pull request on Windows, Linux (Python
3.10 to 3.13) and macOS; the accuracy tests run for pull requests into `main`.
`tests/helpers/kicad_fake.py` stands in for each KiCad version from 9.0.0 to 10.0.6.

### Codebase Layout

| Path | Purpose |
|---|---|
| `kipi_entry.py`, `plugin.json` | KiCad IPC plugin action (runs KiPI as the plugin) |
| `kipi/__main__.py` | `python -m kipi` / `kipi`: standalone, or the plugin with `KIPI_HOST=plugin` |
| `kipi_launcher.py`, `.pyw` | Standalone launcher: sets up KiPI's environment on the first start, then starts KiPI in it |
| `kipi/launcher/` | The launcher itself, standard library only: environment, Python discovery, package list, installer and setup screens |
| `kipi/host.py` | Plugin or standalone: how KiPI was started |
| `kipi/core/` | Shared by every analysis: board model, copper geometry, meshing, sparse solvers, 3D meshes, progress reporting |
| `kipi/sources/` | Where boards come from: the running KiCad (IPC), `.kicad_pcb` files, ODB++, HyperLynx and snapshots |
| `kipi/dc/` | DC analysis: conduction FEM, power-tree setup and solve, thermal, reports, results views and panel |
| `kipi/ac/` | AC analysis: PDN impedance over frequency, field solver and sweep, targets, transient, report and panel |
| `kipi/noise/` | Coupled Noise analysis: waveforms, hot loop parasitics, coupling extraction, victim solves, report and panel |
| `kipi/gui/` | PySide6 application shell shared by every mode: main window, mode switch, 3D viewer, worker threads, About and Help |
| `docs/help.html` | The user guide opened by **Help**; a single offline HTML file |
| `docs/solver-guide.html` | The Solver Guide: PI background and how the solvers work |
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
your own design work is fine. The meshing code is isolated in `kipi/core/mesh/` for this reason.

The other dependencies are under permissive licenses, apart from PySide6 (LGPL-3.0). All of
them are compatible with the GPL.

The author of KiPI prohibits the package from inclusion in commercial software. Use it, fork it, and modify it to your heart's content.
