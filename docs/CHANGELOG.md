# Changelog

All notable changes to KiPI, newest first. Each minor version is one development milestone.
The code's version number went from 0.1.0 straight to 0.5.0, so 0.2.0 to 0.4.0 name
milestones rather than versions that were ever set.

## 0.9.0 (in development)

### Added
- **Drawings in tooltips** (#116). Tooltips on fields that are easier to see than to read
  show a small drawing under their text, coloured to match the window (on a background a
  shade darker), with the field's part picked out: the capacitor model (C, ESL and ESR in
  series and the impedance curve each sets), the DC bias curve (voltage rating, kept at
  rating), aging and the dielectric (its code decoded, and typical temperature curves);
  the VRM types, load line, dropout and input range, DC accuracy, control loop bandwidth,
  phases and remote sense; the VRM's output, input and ground pins; a range's V, ±V or %
  unit against the nominal voltage (load tolerance, VRM input range and DC accuracy); a
  load's current split (equal or per pin group); a series link's side A and B pins, DCR
  and ESL; a probe's target (flat up to a frequency, or custom points); and the mesh's max
  edge, via grading and min edge. The drawings live in one folder, `kipi/gui/graphics/`,
  as SVG templates; where Qt cannot render them, the tooltip is its text alone.
- **3D views in DC PDF reports** (#133). A PDF can't show the interactive 3D views, so
  they go inside it as an attached web page of their own, `<report>_3d.html` (Plotly and
  the views only, about 1–2 MB compressed). A bold **3D Results View** link at the top of
  the report and under each net group opens it in the web browser: the link itself in
  Acrobat, Foxit and PDF-XChange (through Acrobat's JavaScript), and a paperclip beside it
  in Firefox and Okular. It is also in the viewer's attachments panel (Acrobat opens the
  PDF on that panel). The static views stay as they are. Like the notes field, it is added
  to Qt's file as an incremental update; should that fail, the page is saved beside the
  PDF instead and the links open it there. A PDF's own 3D (U3D or PRC) was ruled out: only Acrobat
  shows it, with 3D off by default.
- **Load models** (#17). A part's supplies in a YAML file of their own (`.yml` or `.yaml`,
  nothing else): for each supply its pins and ground pins, current or pin groups,
  tolerance (V, ±V or %) and heat, and for AC its target (flat or custom points) or the
  load step, slew rate and AC tolerance a target is worked out from. A **Model ▾** button
  under the DC Loads and AC Probes lists exports a part's model (every load and probe on
  one reference designator, one supply each, so parts with several supplies keep them
  all), applies a model to any number of loads or probes at once (each from the supply
  chosen beside it; matches by pins or name come ticked), and adds a model's loads or
  probes on every part ticked that has its pins.
- **KiPI in Task Manager** (#124). KiPI shows as *KiPI* with its icon, not as *Python*, in
  Windows Task Manager, macOS Activity Monitor and Linux System Monitor / top / ps. Task
  Manager names an app by the program that owns its window, so on Windows KiPI (plugin or
  standalone) starts again at once on `KiPI.exe`: a copy of its venv's base Python with
  KiPI's icon and version information, made in the venv's `Scripts` folder with the Python
  DLLs beside it, and remade when Python or KiPI changes. A copy that does not start is
  remembered, and KiPI runs on `pythonw.exe` as before. On Linux the process is renamed
  (`prctl`); on macOS LaunchServices is given KiPI's name.
- **Ranges in volts from nominal** (#131). A load's tolerance and a VRM's input range and
  DC accuracy take a third unit, **±V**, beside V and %: volts below and above the nominal
  voltage (e.g. min -0.1 V, max +0.15 V), shown as "±0.1 V" or "-0.1 / +0.15 V". Switching
  between the three converts the values; the tolerance drawing shows all three.
- **Stackup cross-section** (#104). Beside the Stackup window's table, the layers are drawn
  to scale, top to bottom: copper in its layer colour, dielectrics in a colour for their
  material, each labelled with its thickness in the window's unit. The drawing follows
  thicknesses as they are typed. Layers too thin to see are drawn 3 px tall (the label
  gives the real thickness), and clicking a layer goes to its thickness in the table.
- **Signs of life while KiPI works** (#111). The status bar shows a turning spinner with
  the operation's name and the time it has taken while any task runs, part 3D models
  loading included. Each new stage moves the progress bar until it reports how far it is,
  instead of leaving the last stage's full bar still. The window shows the wait cursor
  while it draws a task's results, saves a snapshot or applies a stackup change, and the
  CSV and VTK exports now run in the background, with the spinner and progress bar.
- **Keyboard shortcuts** (#105). In the 3D view, Home snaps to the iso view and Z /
  Shift+Z to the top / bottom view. Anywhere in the window: Ctrl+O open, F5 reload,
  Ctrl+S save snapshot, F12 screenshot, Ctrl+Shift+O open results, Ctrl+L stackup,
  Ctrl+T power tree, Ctrl+1 / Ctrl+2 DC / AC mode, Ctrl+Enter solve, Esc cancel the
  running operation (only while one runs) and F1 help. Each tooltip names its key, and the
  user guide lists them all.
- **User notes in the HTML reports** (#128). The DC and AC reports have an editable User
  notes box at the end, after every result. A report opened from disk cannot save
  itself, so the notes are kept in the browser as they are typed (per file, restored when it is opened again),
  and *Save a copy with notes* downloads the report with them written in, plots and all, to
  keep or share. The box is left out of a printout while it is empty.
- **The standalone launcher has the KiPI icon** instead of Python's (#127). A `.py` file
  cannot carry an icon of its own, so:
  - Windows: the launcher keeps a `KiPI.lnk` shortcut with the KiPI icon in the KiPI
    folder (made on the first start, and again only if the folder or Python moves), and
    the folder itself shows the KiPI icon (`desktop.ini` now points at the icon relative
    to the folder, and the launcher sets the attributes Explorer needs, which git and
    unzipping drop). The setup window has the KiPI icon and shares KiPI's taskbar button;
    pinning it pins the launcher.
  - macOS: `--create-shortcut` now makes `~/Applications/KiPI.app`, with the KiPI icon,
    that runs the launcher, instead of saying it is not supported.
  - Linux: the setup window and the `--create-shortcut` menu entry match, so the panel
    shows the KiPI icon; KiPI no longer replaces that menu entry with its hidden one.
- **PDF reports** (#119). The DC and AC panels' *HTML report* tick is now *Report*, with a
  choice of HTML or PDF beside it (saved with the setup; older setups keep HTML). The PDF
  has the same tables and maps, printed by Qt (already one of KiPI's packages), with
  images of the AC impedance plot and board map. It leaves out the sortable columns, and
  the DC 3D views go in it as an attachment (#133, below). Its User notes box is a fillable form field: type in
  it in any PDF viewer that fills forms (Acrobat, web browsers, Preview)
  and save the PDF to keep the notes; printed, it is a box to write in. Qt cannot make
  form fields, so KiPI adds the field to Qt's file as an incremental update, and should
  that fail the box stays without it. A heading that would end a page starts the next
  one instead, with the table or text it heads. It opens in the PDF viewer. Where a
  PDF cannot be made, KiPI writes the HTML report instead and the log says why. The
  *Report* button is now *Open report*.
- **Guess models** (#130), beside *3D Models*: for a board with no KiCad board file to
  read models from (an ODB++ job, a snapshot opened away from its board), or parts whose
  models KiCad cannot find, it picks models from the library KiCad installs by each part's
  footprint name. It looks for the whole name, a name the part's ends with (ODB++ from
  KiCad names parts `Capacitor_SMD_C_0402_1005Metric`), the name it starts with (a
  `_HandSolder` variant), a chip resistor, capacitor or inductor's size code
  (`CAPC1005X55N`, `C0402`, `R0805`), and last the closest name. KiPI writes a board of just
  those parts, each turned and on its side as on the board, and kicad-cli places the models
  as it does for a KiCad board. It is a guess, so it runs only when clicked: a model not
  found by the whole name is left out if it is far larger or smaller than the part's pads,
  and the log names each one kept. Opening or reloading a board drops them. Standalone
  KiPI now shows part 3D models too when KiCad is installed. Footprints now record their
  rotation (ODB++ and KiCad boards, and snapshots); older snapshots take it from the pads.
  Importing a STEP file of the whole board is not done yet.

### Changed
- **The 3D view in DC reports is lit evenly** (#122): Plotly shaded each flat facet of the
  copper, and the long thin triangles left by thinning the mesh came out in a patchwork of
  dark ones. The color is now the voltage alone.
- **DC accuracy and Control loop bandwidth tooltips** use the terms datasheets do (#135): DC
  error or DC tolerance, from the reference accuracy, feedback resistor tolerances and
  leakage into the FB pin; loop gain crossover, gain crossover or control loop crossover
  frequency, set by the total output capacitance and the loop tuning.
- **Snap to selected works for capacitor models.** Picking a model in AC mode's
  Capacitors tab marks every part it names in amber and, with Snap to selected ticked,
  brings the view to them; clicking a capacitor in 3D selects its model.
- **Loss tangent and Default εr moved to the Stackup window** (#134), from the AC setup's
  Mesh tab, beside the copper Resistivity and TCR. Like those they are setup settings, so
  they can be changed while the stackup is locked; setups keep them as before.
- **PARDISO is installed by default** where Intel MKL exists (Windows and Linux on x86-64),
  with an opt-out (#129).
  - Plugin: KiCad installs `requirements.txt` on every reload without asking, so PARDISO
    stays out of it. The first time KiPI opens it shows what PARDISO would add (each
    package and its size), ticked, and installs it with the plugin environment's pip
    (wheels only, as KiCad does) behind a progress dialog that can be canceled. Unticking
    is remembered outside the environment. *About → Manage dependencies…* now appears in
    the plugin too, to add or remove it; a removal waits for the next start, before MKL is
    loaded, and KiPI stops using it meanwhile. After *Recreate Plugin Environment* KiPI asks
    again unless it was turned down. A failed or canceled install leaves KiPI on SciPy's
    solver and is not asked again on every start.
  - Standalone: the launcher's first setup has PARDISO ticked; later setups keep what was
    chosen.

## 0.8.0: plugin and standalone modes

First milestone of running KiPI on its own, outside KiCad (#45). The plugin works as before.

### Added
- The part selected in the setup, or by clicking its name in 3D, **blinks** red and amber
  for a few seconds before it stays amber, so it is easy to find among the PI Models marks.
  A new **Snap to selected** tick in the View box chooses whether picking a part in the
  setup's lists brings the 3D view to it (ticked, as before) or leaves the view where it
  is. It is available only while **PI Models** is ticked, applies to DC and AC mode and is
  remembered between sessions.
- **PI Models** marks AC mode's parts too, having marked only DC mode's: the setup's VRMs,
  series links, probes (violet) and capacitors with a model, on their pads on the nets
  shown. The probes take the DC loads' place, the loads having no part in an impedance
  sweep. The tick, the dot sizes and the names work as in DC mode, and so does picking
  (#96): a VRM, series link or probe clicked in the AC setup's lists is marked in amber and
  brought into view, and clicking its name in 3D selects it in the AC setup.
- Capacitor models have an optional **Manufacturing tolerance** (% of nominal), and the
  Sweep tab a **Capacitor manufacturing tolerance** tick that solves every model which has
  one at the low end of it: the tolerance is subtracted, so a ±10 % model keeps 90 % of its
  capacitance, the worst case for a PDN. It multiplies with the DC bias estimate, aging and
  temperature, and the Results notes and HTML report say which models were derated.
- Target calculator: **Subtract DC drop at the load** takes the DC drop at the selected
  load's pins off the AC voltage tolerance, from the last power-tree solve: its effective
  voltage (supply minus ground pins) against the set voltage of the VRM driving it. It needs
  a DC solve, and stays disabled, with the reason in its tooltip, until that load has a
  result. A boost (a load above the set voltage) is subtracted the same way.
- AC Probes tab: **From DC load**, next to Add, Duplicate and Remove, adds a probe on the
  same supply and ground nets, parts and pins as the DC load picked from its dropdown. A
  load without ground pins gets its part's pins on its ground net.
- **From DC load** also lists the input of every LDO and switcher, one entry per phase
  ("VR4 phase 2 input"), and adds a probe on that phase's input and ground pins (#117). The
  target calculator treats such a probe's regulator as its DC load: **Copy tolerance**
  takes the tighter side of the regulator's input range (in volts, or in % of its input
  rail), and **Copy current** and **Subtract DC drop** take its input current and the drop
  at its input pins from the last power-tree solve.
- **Standalone mode.** Started from KiCad's toolbar, KiPI runs as the plugin and reads the
  board open in the PCB editor. Started any other way (`python -m kipi`, or the `kipi`
  command of an installed package), it runs standalone: it opens board files and needs no
  KiCad.
  - `python -m kipi [board]` opens that board; a file KiPI cannot read stops it with a
    message.
  - The toolbar has **Open…** and **Reload** in place of *Load from KiCad*, *Refill zones*
    and *Open snapshot…*. *Select in KiCad* and the part *Models* box are hidden, and the
    copper uses KiPI's default layer colors.
  - Standalone opens KiPI snapshots (`.kipi.json.gz`), KiCad board files and ODB++ jobs
    (below). Reading HYP files comes next.
  - The title bar says *(standalone)*, and the welcome screen and About show the mode.
  - The plugin and standalone KiPI each allow one window of their own, so an open
    standalone KiPI never takes over KiCad's KiPI button.
  - `KIPI_HOST=plugin` (or `standalone`) overrides how KiPI tells the two apart, e.g. to run
    the plugin workflow from a terminal.
- **Standalone launcher** (`kipi_launcher.py`; double-click `kipi_launcher.pyw` on
  Windows). The KiPI folder can be anywhere: the first start sets up an environment of
  KiPI's own (`%LOCALAPPDATA%\KiPI`, `~/Library/Application Support/KiPI` or
  `~/.local/share/kipi`), and later starts open KiPI straight away.
  - The setup window lists every package that will be installed, with its version and
    download size as PyPI gives them for this computer, and the totals to download and on
    disk. Nothing is installed until you click *Install*. Offline, it shows the list saved
    with the release.
  - On Windows and Linux x86-64 it offers PARDISO, with its size, pros and cons; elsewhere
    it says why not.
  - It finds a Python 3.10 to 3.13 to use (the py launcher, `python3.N`, or one uv
    installed) and says what is wrong with the others, such as Debian's missing
    `python3-venv`. It installs with uv when uv is on the PATH, else venv and pip, and with
    pip again if uv fails. Progress, the installer's output and Cancel are in the window.
  - A setup that was canceled or failed is picked up on the next start, and so is a new
    KiPI that needs different packages. The launcher keeps its own logs.
  - With no tkinter (KiCad's bundled Python has none) or `--console`, setup runs in the
    terminal; with neither a window nor a terminal, a message says how to start it.
  - `--setup`, `--add-pardiso`, `--remove-pardiso`, `--repair` and `--reset` manage the
    environment, and `--create-shortcut` adds KiPI to the Start menu (Windows) or the
    applications menu (Linux).
  - **About → Manage dependencies…** in standalone KiPI closes KiPI, opens the setup, and
    starts KiPI again afterwards.
  - `python -m kipi` in a Python without KiPI's packages says which are missing and to use
    the launcher, instead of a traceback.
- **Solver Guide** (#92, `docs/solver-guide.html`, served by GitHub Pages): a page
  for newcomers on what power integrity analysis is and why it matters. It covers what
  each analysis simulates, how the triangle mesh and the DC, electro-thermal and AC solvers
  work, and what DC tolerance analysis and PDN impedance mean. It also explains how heat
  raises copper resistance, how dielectric, DC bias, aging and tolerance shrink a
  capacitor's effective value, what PARDISO gains over the default solvers, and the
  problems PI simulation finds. It is linked at the top of the README, from the About
  window and from the user guide.
- **About → Check for updates** (#97) asks GitHub for the newest tagged release and says
  whether it is newer than the KiPI running, with a link to its release page. It only asks
  when clicked; offline (or rate-limited) it says it could not reach GitHub.
- **KiCad board files without KiCad.** Standalone **Open…** (and `python -m kipi
  board.kicad_pcb`) reads KiCad 7 to 10 `.kicad_pcb` files directly, giving the same board as
  the plugin reads from the running KiCad: the same stackup, copper, pad shapes and item ids,
  so a setup made in the plugin opens with the file and the other way round.
  - Every pad shape (round, rectangle, rounded and chamfered rectangle, oval, trapezoid,
    custom), KiCad 9 padstacks with their own inner and back shapes, plated pads that drop
    unconnected layers, blind and micro vias, arc tracks, copper drawings with a net, and
    footprints on either side.
  - Net classes come from the project's `.kicad_pro` (assigned nets and patterns).
  - Zones are read as KiCad last filled them. A zone saved unfilled has no copper, and the
    board summary's *Placeholders* says so; fill zones in KiCad (B) and save first.
  - When the open board file is saved (in KiCad or anywhere), KiPI offers to read it again.
  - **Open…** has a menu of the boards opened lately.
  - Checked against the KiCad API on real boards: identical copper, and identical DC results.
- **ODB++ jobs.** Standalone **Open…** reads ODB++ as a `.zip`, `.tgz` or `.tar.gz`, or an
  unpacked job (by its folder, or its `matrix/matrix` file).
  - Pads, traces, arcs, vias (with their spans from the drill layers) and filled planes,
    tied to nets and part pins through the job's netlist; the stackup from the layer matrix
    and each layer's attributes.
  - Pads keep their part and pin, so setups (which name parts, pins and nets) carry over;
    the setup is found by the job's name.
  - What the job does not give is listed under *Placeholders*: a dielectric with no
    thickness, copper drawings whose net was guessed from the copper they touch.
  - KiCad's export at its default 2 decimals rounds coordinates to 10 µm; copper thickness
    is then taken from the copper weight. KiCad's quirks are handled: bottom pads already
    mirrored, its names for pads with no number.
  - Checked against the KiCad API on a real 8-layer board exported by KiCad 10: every
    track, via and pad the same, and DC drops within 3 parts per million.
- **Where the board came from, and its placeholders.** The board summary in the Info box
  names the source ("From KiCad PCB editor 10.0.7") and the snapshot file it was opened from,
  and lists under *Placeholders* each value KiPI had to fill in because the source did not
  give it: a default stackup, a 2-layer guess from a KiCad too old to list its layers, or
  pads kept on every layer when KiCad cannot say which layers plated pads are on. Snapshots
  keep both; older snapshots read as coming from KiCad.
- Loads not connected by copper (#88). Before a DC solve, KiPI checks each load pin for a path
  to its VRM (or ground reference) through copper or series links, instead of stopping at
  the first one without. Unconnected pins draw nothing and the load's current goes through
  its other pins; a load with no supply pin, or no ground pin, connected is left out of the
  solve and shows as **DISCONNECTED**. The Notes tab lists every unconnected pin. A rail
  with no load connected at all still stops the solve.
- The Target Impedance/Frequency Calculator has a **Copy tolerance from DC load** button. It
  fills the AC voltage tolerance from a DC load on the probe's rail: ± its tolerance % of the
  voltage of the VRM feeding it (#87).
- VRM **Load line** (#94). A VRM with a load line, in mΩ, regulates to its set voltage less
  the load line times its output current: the current its connected loads and the VRMs it
  feeds actually draw, so disconnected loads add no droop. It is iterated with the rest of
  the rail, works with remote sense, multi-phase VRMs and negative rails, and shows in the
  Notes tab, the power tree diagram, the HTML report and the VRM CSV (**V target**). Drops
  are still measured from the set voltage, so the droop counts against load tolerances.
- Editable **color scale** limits (#101). The Results pane has a minimum and maximum for the
  3D color scale, starting on the result's whole range; typed limits are kept per net group
  and field (temperature limits for every group) until **Auto**.
- The VRM, load or series link selected in the setup is marked in amber in the 3D view.
  Clicking it in its list ticks **PI Models** and brings the view to it, and clicking a
  part's name (or dot) in 3D selects it in the setup (#96).
- Capacitor **DC bias** (#74). A capacitor model with a **Voltage rating** (and not a C0G
  dielectric) is derated by DC bias: with the capacitance it **Keeps at rating** (40 % by
  default), each of its parts is derated by the DC voltage across it, from the last DC
  solve or else the rail's VRM set point. **Capacitor DC bias** in the Sweep tab turns it
  off for every model. **Models from values** reads the rating from values like
  `10uF 6.3V X5R` for class II dielectrics. The Results notes say where the voltages came from, and
  the HTML report lists each model's capacitance at its bias.
- Capacitor **aging** (#109). A capacitor model has a **Dielectric**, which fills in its
  typical **Aging** rate (% per decade of hours; editable). With **Capacitor aging** ticked
  in the Sweep tab, each model with a dielectric loses its rate for each decade of **Hours
  operated** past 1000 hours (ten years by default; set by typing or with a log-scale
  slider, shown in years), on top of its DC bias. **Models from
  values** reads the dielectric from the value, and the Results notes and HTML report show
  the derated capacitance.
- AC solves at a **temperature** (#77). **Temperature** in the Sweep tab sets the copper's
  resistivity (by its temperature coefficient, with skin effect) and the capacitance of
  models with a dielectric (by a typical curve for each dielectric): **Fixed** at one board
  temperature, or **From DC thermal co-simulation**, from the copper temperatures of the
  last DC solve with thermal co-simulation or of DC results opened from a file. The Results
  notes and the HTML report give the temperatures used.
- KiPI can be **pinned to the Windows taskbar**. Its taskbar button used to refuse
  pinning, as only KiCad could start KiPI; a pinned button now starts standalone KiPI
  through the launcher (from the plugin too). The launcher's Start menu shortcut carries
  KiPI's taskbar ID, so pinning it and the open KiPI share one button. Without the
  launcher next to KiPI, pinning stays off.
- The Target Impedance/Frequency Calculator has a **Copy current from DC load** button
  beside the load step. It fills the load step from the current of a DC load on the
  probe's rail, the probe's own part first, with a menu when there are several.
- LDOs and switchers have a **DC accuracy** row under the input range: min and max of the
  output's DC error, in % of the set voltage or in volts. The Target Impedance/Frequency
  Calculator has a **Subtract VRM DC accuracy** button under the AC voltage tolerance,
  which takes the larger side of the accuracy of the VRM driving the probe's rail off
  the tolerance (a menu when several VRMs drive it).
- **Via plating rules** (#89, from PR #42 by @Tomachie). The Mesh tab's single *Barrel
  plating* value becomes a table of rules, first match wins: hole type (through, blind,
  buried, microvia, component hole or any), a drill-size range, the plating, and what a
  filled hole of that rule is filled with (copper, conductive or non-conductive epoxy). The
  defaults are IPC-6012 Class 2 minimum average plating (through and blind 20 µm, buried
  13 µm, microvia 12 µm). The DC and AC solves, the Notes tab and both HTML reports use them.
- **Import a Tomachie power tree** (#120, from PR #43 by @Tomachie). **Import…** beside the
  DC setup's Save fills the VRMs, loads and DC connections from a Tomachie `.pdn.json`:
  sources; LDOs and switchers with their input pins, efficiency, quiescent current, dropout
  and input range; load switches as DC connections with their on-resistance; series parts
  with their datasheet DC resistance; each load's current, and a tolerance from its rail's
  stated range, raised to the datasheet's minimum supply voltage where that is higher.
  Schematic nets are matched to board nets through the pads. KiPI takes one current per load
  and one resistance per DC connection, so the typical is used; the log lists each minimum
  and maximum left out, each current the file does not know (imported as 0 A) and whatever
  could not be placed on the board. Settings, thermal and the AC setup are kept.
- **Filled vias** (#89). A via the KiCad board marks as filled carries its core too when its
  rule's fill conducts: copper makes the whole hole copper, conductive epoxy adds its core at
  the resistivity entered. A via whose fill is left to the design rules is solved unfilled,
  and the Notes tab counts them. Read in plugin mode and from `.kicad_pcb` files; other
  formats do not say, so their vias are solved unfilled.
- **Via pads per layer** (#89). Each via's pad is read per layer from its padstack (normal,
  front / inner / back, or custom), and a pad removed from an unused layer is not counted as
  copper. In a `.kicad_pcb` file a via set to remove unused layers loses its pad on each
  layer where no track, pad or zone of its net meets it, keeping the outer ones if it keeps
  end layers.
- Loads have an optional **Heat** (W) for the electro-thermal solve: with load power
  included, it replaces the power the load draws, for a part that passes most of its power
  on (a load switch, LED or motor driver). The Notes tab and the HTML report list the heat
  put into the board by source (copper, vias, DC connections, VRM conversion, loads).

### Changed
- README and user guide screenshots retaken for 0.8.0 (#118): the main window, setup tabs,
  DC results, HTML report and AC views show the current interface. New screenshots of the
  launcher's setup window, PI Models, the Power tree and Stackup windows, the via plating
  rules and the AC Board map.
- Stackup window: **Lock to imported values** now also locks each copper layer's conductor
  and its resistivity in the table, which could be changed while locked. Locking keeps the
  conductor chosen rather than resetting it; the board's Resistivity and TCR stay editable.
- Selecting a part in the setup no longer ticks **PI Models** in the 3D view; the marks
  stay as you set them.
- Setups and results saved with the single *Barrel plating* value load it as the plating of
  every default via rule, so they solve as before. New setups use the IPC-6012 defaults
  (20 µm for through vias, was 25 µm), which raises barrel resistance by about a quarter.
- The Target Impedance/Frequency Calculator copies the tolerance and load step from the
  DC load on the probe's pins, with no menu. A **DC load** dropdown picks among the loads on
  the probe's part when none sits on its exact supply and ground pins.
- Every VRM field can be edited from both the DC and the AC setup. Fields only one
  analysis uses (control loop bandwidth and DC accuracy in DC; efficiency, input range and
  the like in AC) are no longer grayed out; their tooltip says which analysis uses them.
- A capacitor model no longer has an **Estimate DC bias** box: a **Voltage rating** turns
  the DC bias estimate on. **Models from values** keeps the rating only for class II
  ceramics (X5R, X7R…), and a C0G model is never derated.
- A load's **tolerance has a separate min and max**, on one row like the VRM input range,
  each in % from the VRM set point (e.g. -3 % / +5 %) or in volts (e.g. 3.135 to 3.465 V),
  and either one optional. A load passes when every pin is within it. The results table,
  the report and the power tree show the tolerance as set; the loads CSV has `tolerance`,
  `tolerance_min_V` and `tolerance_max_V` in place of `tolerance_pct`. Copying a tolerance
  into the target calculator takes its tighter side. Setups and results from earlier
  versions keep their ± % tolerances.
- A VRM's **input min and max share one row**, "Input range", and can be given in V or in
  % from the input rail's nominal voltage, the set voltage of the VRM driving it (e.g.
  -10 % / +10 %). Switching the unit converts the values when that VRM is known, and
  clears them when not. A range in % with no VRM on the input rail is a setup error.
- The Target Impedance/Frequency Calculator's slew rate starts at 10 A/µs instead of
  empty, and the calculator no longer cites the target impedance paper (the help still
  does).
- `python -m kipi` with no arguments starts standalone instead of connecting to the running
  KiCad. Use `KIPI_HOST=plugin python -m kipi` for the old behavior; `--dump` still
  extracts from the running KiCad.
- `kicad-python` is now the `kicad` extra of the Python package
  (`pip install -e "<KiPI folder>[kicad]"`), since standalone KiPI does not need it. `requirements.txt`, which KiCad installs for the
  plugin, still includes it.
- **Laminate k** is gone from the thermal settings (#90). Each dielectric's thermal
  conductivity is now edited in the Stackup window's k column instead: choosing a material
  still sets its typical value, and it can be typed over while unlocked. The setup file
  keeps it with the other stackup edits. A setup with a Laminate k opens with that value as
  every dielectric's k.
- The Airflow tooltip and help say the air flows parallel to the board in a fixed direction,
  cooling every point equally (#98).
- The 3D color bar sits against the right edge of the view, leaving a small gap, so it
  covers less of the board; its title ends at the bar's right edge so long titles stay in
  view (#103).
- Hiding copper layers no longer rescales the result colors: the scale covers every layer
  until you set its limits (#101).
- KiPI answers its toolbar button at once (#55). The welcome screen now comes up first and
  says what KiPI is doing (connecting to KiCad, loading its packages, opening the window)
  until the window is open, instead of appearing only after everything has loaded. The
  packages load on a thread, so it keeps animating. The first start after a reboot or
  after KiCad starts can still take a while, since thousands of files are read from a
  cold disk (16 s on one Windows machine, 1 s once cached), and the log records how long
  each part took.
- KiPI's window comes to the front when it opens, instead of behind KiCad (#112). On
  Windows it takes the foreground from KiCad, and, if Windows refuses, it still opens on top
  of the other windows. It stays in front after the welcome screen closes, unless you have
  switched to another app in the meantime.
- The standalone launcher installs KiPI's packages with uv even when uv is not on the PATH
  (#113). It uses uvx or pipx to fetch uv, and falls back to venv and pip as before when
  none of them is available. The setup log names the route taken, for example *uv (pipx)*.
- Setups are saved as YAML, `<board>.kipi-setup.yml`, which is easier to read, diff and
  edit by hand (#76). A `.kipi-setup.json` from an earlier KiPI still opens when there is no
  `.yml` setup, with a log warning that JSON setups are deprecated and will stop being
  read in a future version. The next save writes the `.yml` file and leaves the JSON file
  as it was. KiPI now needs PyYAML. In an environment that does not have it yet, setups
  stay JSON. The example and validation setups are now YAML.
- Results files (`.kipi-results`, `.kipi-ac-results`) hold JSON and NumPy arrays in a zip
  instead of a Python pickle (#99). They open on any computer and cannot run code, so they
  are safe to share. Opening one builds only KiPI's own result classes, and arrays are read
  without pickled objects.
  - Saving is about 4x faster and the files about 3x smaller, because the 3D views are no
    longer saved: they are rebuilt from the solution when the file opens. On the multiphase
    example, saving takes 0.7 s instead of 2.8 s, for 30 MB instead of 92 MB. Saving and
    opening DC results run in the background with progress, like a solve.
  - A failed save leaves an earlier file of the same name as it was.
  - Results saved by KiPI 0.7 and earlier still open, but KiPI first warns that a pickle
    can run code and asks whether to open it. It then reads the pickle with an allowlist of
    the classes results hold, and logs that the format is deprecated. Saving again writes
    the new format.
  - Electro-thermal results save and reopen in the new format too (#50).
  - DC results files are named for what was solved: `.kipi-dc-results`, or
    `.kipi-dc-thermal-results` after an electro-thermal solve. **Open results…** in DC mode
    takes both, and `.kipi-results` files from earlier versions.
- The via current limit starts at 1 A rather than none, a placeholder that marks vias PASS or
  FAIL from the first solve (#114). Set your fab's rating, or 0 for none. A setup saved
  with no limit keeps none.
- **3D Models** shows every part on the board, not only the setup's parts on the nets shown:
  the PI Models markers now say which parts are in the setup (#96).
- New phases of a multi-phase VRM follow a board annotated by schematic sheet (#91): phase 1
  on `L101`, `Q101` and `Q102` gives phase 2 `L201`, `Q201` and `Q202`, not the next parts in
  sequence on phase 1's own sheet. A step the earlier phases set is kept too (`Q2`, `Q4` →
  `Q6`).

### Fixed
- The DC HTML report's 3D view no longer draws triangles across holes and off the board,
  which on a large board stretched the axes far past it (#102). The mesh is thinned by
  dropping interior vertices only, so the copper keeps its outline, holes and solved
  voltages; thinning also runs about ten times faster.

### Internal
- Board sources are kept apart from the rest of KiPI. `kipi/sources/` holds where boards
  come from: `kicad_ipc/` (the running KiCad, moved from `kipi/core/kicad_io/`), the
  snapshot file, and a registry of the file formats Open reads. The window sees only a
  `BoardSource` (read, re-read, refill, select) and an `InstalledTool` (layer colors and
  part models). `tests/core/test_layering.py` fails if kipy is imported outside the KiCad
  source, or if core, DC, AC or the GUI import a source directly.
- What any board reader shares moved into `kipi/core`: KiPI's copper layer ids
  (`core/model/layers.py`, equal to KiCad's), stackup building and the default stackup
  (`core/model/stackup.py`), snapshot I/O (`core/model/snapshot_io.py`), arc discretization
  (`core/geometry/arcs.py`) and the default layer colors (`core/viz/layer_colors.py`).
  Results saved by earlier versions still open.
- `kipi/host.py` says how KiPI was started; the entry points set it.
- `kipi/sources/kicad_pcb/` is the board-file reader. Its tests compare it item by item with
  the KiCad API's reading of a fixture board KiCad saved (`tests/data/kicad_pcb/`, made by
  `make_fixtures.py`). The stroke, bezier and outline helpers both KiCad readers use moved
  to `core/geometry/shapes.py`, and the pad shapes cut as KiCad cuts them to
  `core/geometry/pad_shapes.py`. Sources say which files they read
  (`BoardSource.files()`), which the window watches.
- `kipi/sources/odbpp/` is the ODB++ reader, tested against KiCad's ODB++ exports of the
  same fixture board (`tests/data/odbpp/`, also made by `make_fixtures.py`). The registry
  lets a format be a folder as well as a file.
- `kipi/launcher/` is the launcher, standard library only (a test checks).
  `requirements-standalone.txt` is what it installs, and
  `.github/scripts/dep_sizes.py` writes the package list it shows offline
  (`kipi/launcher/deps_manifest.json`), for Windows, Linux and macOS; a test fails when
  that list is stale. Linux on ARM is left out: Triangle has no prebuilt package there, so
  pip builds it.
- The board heat model's log line names its direct solver, as the copper solves' lines
  already did, and a test checks that DC and thermal solves both use PARDISO wherever it is
  installed (#108).
- CI no longer runs for hours (#115). The macOS test job hung in the launcher's setup-window
  tests: a click handled while the window was still being drawn ended the window's loop
  before it started, so it waited forever. The setup window now checks for an answer before
  waiting (this could also happen to a user on a slow machine), and the tests end the window
  if a check fails rather than hang. Every test now has a 5-minute limit (pytest-timeout)
  and every job a limit of its own, a new push cancels that branch's older run, and pushes
  that change only Markdown files skip CI. The tests themselves take about a minute; the
  rest of a job is installing packages.
- CI has a **KiCad versions** job with a step for each KiCad release KiPI is tested against
  (9.0.0 to 10.0.6, through the fake KiCad that answers as each one does) and for board
  files saved by KiCad 9 and 10, so the pipeline shows which versions pass (#106).
  `tests/core/test_ci.py` fails if the steps and the tested versions drift apart.

## 0.7.2

### Added
- **PI Models in the 3D view** (#84): in DC mode, a new **PI Models** checkbox under
  Display marks the setup's VRMs (green), loads (cyan), series links (magenta) and
  capacitors with a model (white) on the copper, with a dot on each of their pads on the
  nets shown (smaller on ground) and the part's name. It starts unticked; the marks follow
  setup edits and are hidden over results. The **Models** checkbox is now **3D Models**.
- **Solver accuracy tests** (#70): eight validation boards in `examples/validation`
  (thin and arc tracks, right-angle routes, a layer change through vias, planes, a power
  chain with a series link, and AC plane, decoupling-capacitor and ferrite boards), each
  solved and compared with hand calculations. They run in `tests/accuracy`, as a separate
  final CI job on pull requests into `main`, and open in KiPI like any snapshot.
- **Click-to-sort tables everywhere** (#85): the capacitor parts table, a probe's custom
  target points and a load's pin groups sort by a clicked column header, as do every table
  in the DC and AC HTML reports. Values with SI prefixes sort by value (100 nF before
  10 µF, 4u7 as 4.7 µ), references sort naturally (C9 before C10), ticked parts can be
  sorted first, and blanks go last. A table keeps its order when ticking a part refills it.
- **Footprints in the capacitor parts table** (#64): each capacitor's footprint shows in
  its own column (the library in its tooltip) and the filter matches it. **Tick same
  footprint** and **Tick same value + footprint** join **Tick same value**, so e.g. the
  0402 and 0603 100 nF parts can get their own models. Snapshots now keep each
  footprint's library name; boards saved earlier show it once read again from KiCad.
- **Power tree** toolbar button: a block diagram of the VRMs (each type in its own colour and
  tag), rails, series links, loads and capacitors, laid out in the direction power flows,
  saveable as a PNG. Rails with many loads or capacitor groups pack them into grids shaped
  so the whole tree fits the window (#58).
- Thermal settings: **Include load power dissipation** (off by default) heats the board
  with each load's power, the sum over its supply and ground pins of pin current × pin
  voltage, spread over those pins. The DC report lists each load's and DC connection's
  heat in a new Component power dissipation table (#62).
- **SI prefixes in value fields** (#60). A value can be typed with a prefix and unit, which
  is converted to the field's unit. For example, `10n` or `10 nF` in a µF field, `2 mOhm`
  or `4k7` for a resistance, `1.2 MHz` in a kHz field, or `3.3 V` in a mV field. This
  works in every field whose unit is an SI unit (Ω, F, H, Hz, A, V, W, m). A field keeps
  its usual decimals but shows more when a small typed value needs them, so 10 pF in a µF
  field reads 0.00001 µF instead of rounding to zero. See *Typing values* in the help.
- **Stackup window** (#61). The new **Stackup** toolbar button opens a window listing
  every copper and dielectric layer with its thickness, each dielectric's material, εr and
  in-plane thermal conductivity, and each copper layer's resistivity. Thicknesses show in
  µm, mm, mil or oz. The stackup is locked to KiCad's values by default. Untick **Lock to
  imported values** to try other thicknesses, materials and εr. Each dielectric's material
  is a drop-down of the materials KiPI knows; choosing one sets its typical εr and thermal
  conductivity. Each copper layer has a conductor drop-down too (annealed, electrodeposited
  or rolled-annealed copper, silver, gold, aluminum, nickel or tin), which sets that
  layer's resistivity, TCR and thermal conductivity; the DC solve and the electro-thermal
  model use a TCR and a thermal conductivity per layer. The layers restack, and every DC and AC analysis, the 3D view and the reports use the
  edited stackup. The PI setup keeps it. KiCad's API cannot change a board's stackup yet, so
  edits stay in KiPI.
- **AC Board map tab** (#67). In AC mode a **Board map** tab beside **Impedance** shows the
  report's board map in the GUI: the solver's mesh of the rail and its ground (all layers,
  or one layer at a time, with or without the triangle edges), the board outline, the
  probes, and the capacitors in the model coloured by model, with those left out for want
  of a model crossed. It fills in while the rail solves, the parts first and then the mesh,
  and the Impedance tab comes to the front when the results arrive. Hover a part for its
  model. The view is fixed, fitted to the board. Saved AC results keep the map.
- **Every pin's voltage and current in the DC reports** (#71). The CSV export adds
  `<stem>_pins.csv`, and the HTML report adds a **Pins** section with a collapsible
  table per net group. Both list every pin each group was solved with: VRM output, phase,
  input and sense pins, load supply and ground pins, and DC connection pins. Each row has
  the pin's position, voltage, drop or boost from the set point, and current. VRM output
  pin currents are solved, so they show how the current shares between a VRM's pins. A
  load's current is split equally across its pins, which is how the solver applies it.
  Results saved before 0.7.2 have no pin list.

### Changed
- HTML report file names carry the date and time of the solve
  (`<board>_kipi_report_<date>_<time>.html`,
  `<board>_<rail>_ac_report_<date>_<time>.html`), so a new solve no longer overwrites the
  last report, and **Save results…** suggests a time-stamped `.kipi-results` /
  `.kipi-ac-results` name (#66).
- The DC setup's **Copper** tab is gone. Its resistivity, TCR and per-layer resistivity
  moved to the Stackup window, since the AC solve uses them too.
- The **Cut** plane (axis, slider and **Flip**) is gone from the Display options.
- The multiphase buck example has a 10 µF input capacitor (C20-C23) on VIN beside each
  power stage, with a `10u` model in its setup, so an AC solve of VIN shows decoupling.
  Its setup probes VIN at all four power stages (Q1-Q4).
- HTML reports open in the default web browser as soon as a DC or AC solve writes them
  (Python's `webbrowser` is the fallback where the desktop cannot open the file). **Report**
  still reopens them later.
- The AC HTML report's impedance plot shows |Z| only; the phase plot under it is gone.
  Hovering a curve still gives R and X.
- **Flat current arrows** (#63). The current-density view's arrows are flat 2D arrows
  drawn on each copper layer instead of 3D cone-and-cylinder models. They lie on the
  copper's top face and are repeated on its bottom face, so they show from above and
  below, and they are unlit, so they stay plain white at any viewing angle.
- **Dielectric constant from the KiCad stackup in AC mode** (#65). A dielectric made of
  several sublayers (for example, two prepreg sheets) now gets all of their εr, combined
  in series. Before, the first sublayer's εr was used for the whole thickness. The AC
  solve's log and the HTML report list the εr used for each dielectric and mark where
  *Default εr* stood in.
- The version number is 0.7.2. The 0.7.1 release still reported itself as 0.7.0.
- Tests are grouped into `tests/core`, `tests/dc`, `tests/ac`, `tests/gui` and
  `tests/scripts`, with shared test boards in `tests/helpers` instead of being imported from
  other test files. CI shows each folder's results in its own log group.

### Fixed
- **Online user guide link**: when the local guide is missing, KiPI now points to the
  guide on GitHub Pages (https://drewsum.github.io/KiPI/help.html) rather than to the
  file's HTML source on GitHub.
- Clicking a results table's column header a second time now reverses the sort; it used
  to sort ascending every time, because Qt flips its own sort indicator before the click
  reaches KiPI (#85).
- The AC HTML report no longer leaves a large blank space between the board map and the
  capacitor models. The map's plot shared its id with the section heading, so Plotly drew
  the map inside the heading and left the map's own space empty. The map's height now also
  follows the page width, so a narrow window doesn't leave blank bands above and below the
  board.
- When KiPI cannot read the board's stackup, its default stackup no longer fixes εr at 4.5.
  The AC *Default εr* setting now applies there, as it does for any dielectric the stackup
  gives no value for.

## 0.7.1

### Added
- **Save and open AC results** (#56). *Save results…* in the AC panel writes every rail
  solved so far to a `.kipi-ac-results` file. In AC mode, the toolbar's *Open results…*
  (DC only before) reopens it without solving again: each rail's curves and table come
  back under its VRM, and the HTML report link where the report still exists. Rails
  whose VRM is no longer in the setup are left out and logged.
- DC and AC results files are marked with their kind, and opening one in the wrong mode
  says which mode it belongs to. DC files from earlier versions still open.
- **Project statistics in About** (#59): clones since tracking began, unique cloners over
  the last 14 days, and release downloads. A daily workflow publishes them to the repo's
  `stats` branch, so no token ships with the plugin; the dialog fetches them in the
  background once a session and shows the last ones fetched when offline.
- Each release gets a `KiPI-<version>.zip` to unzip into KiCad's plugins folder, which is
  also what makes its downloads countable. The README's install steps mention it as the
  way to install without git.
- The *Open results…* tooltip and the user guide describe what it opens in each mode, and
  the guide's About entry explains where the statistics come from.

## 0.7.0: AC impedance analysis, faster solves

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
    tolerance, load step and slew rate, and links the paper that introduced the method
    (Smith et al., IEEE Trans. Advanced Packaging, 1999).
  - The VRM is left out of circuit, so the solve gives the open-loop PDN impedance. Targets
    apply only above the VRM's *Control loop bandwidth*, where the converter's loop no
    longer holds the rail. The plot draws them only there, marks the bandwidth, and judges
    PASS/FAIL, worst |Z| and margin only above it. If the bandwidth is at or above the
    target frequency, or above the sweep, the whole target is judged and the results say
    why.
  - Each rail keeps its last result; switching the rail shows its curves without solving
    again. *Export CSV…* writes |Z|, R and X per probe.
  - *HTML report* next to *Solve rail* also writes `<board>_<rail>_ac_report.html`, a
    single offline file like the DC report, and *Report* opens it. It contains the probe
    table with margins and PASS/FAIL, an interactive |Z| and phase plot with the targets and
    each capacitor model's own curve, and a board map of the rail's copper with the probes
    and capacitors. It also lists the capacitor models, series links, settings and solver
    statistics.
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
- **All and None buttons for the copper layers.** They show or hide every layer at once.
- **Layer opacity slider.** *Display → Opacity* draws the copper layers and pads see-through
  (5% to 100%), in the plain and result views, to see the layers beneath. Vias stay solid.
- **KiCad's layer colors.** Colored by layer, the copper and the layer list use the
  copper colors of the PCB editor's color theme, read from KiCad's settings folder
  (`pcbnew.json` and `colors/<theme>.json`) on every load. A built-in, missing or unreadable
  theme, or a layer the theme leaves out, gets KiCad's default theme color; the log names
  the theme used.
- **Welcome screen.** KiPI shows the animated logo, its version, the plugin identifier and
  the versions of Python and its main packages while it starts. It closes on its own after
  a few seconds (or on a click); if it cannot be shown, KiPI starts without it.
- **One KiPI at a time.** Starting KiPI while one is open brings the open one to the front
  instead of opening a second. A KiPI that crashed does not block the next one, and if the
  lock cannot be written KiPI starts anyway.
- **One KiCad at a time.** KiPI refuses to connect, at start or on *Load from KiCad*, while
  more than one KiCad (project manager or standalone PCB editor) is open for this user, and
  names them with their PIDs. If the processes cannot be listed it carries on;
  `KIPI_ALLOW_MULTIPLE_KICAD=1` skips the check.
- **3D models of the modeled parts.** The geometry view draws the KiCad 3D models of the
  parts named in the setup (VRMs, loads, series links and capacitor models) that have a
  pad on a net shown, like the copper. *Display → Models* hides them. Over results they
  start hidden and *Models* shows them; the geometry and result views each keep their own
  setting.
  - KiCad's `kicad-cli` exports them (`pcb export glb`, no board body, just those parts),
    so KiCad resolves the model paths and reads STEP as well as VRML. It reads the saved
    board file, in the background; parts without a model KiCad can find are named in the
    log.
  - The Z scale stretches the board but not the parts; top-side parts sit on the stretched
    board. Clicks go through the models to the copper.
  - Fallbacks: kicad-cli is found through the connected KiCad, the PATH, the usual install
    folders or KiCad's Flatpak (or `KIPI_KICAD_CLI`); an older kicad-cli is asked again
    without the options it does not know. With no kicad-cli or no board file on disk, KiPI
    says so once and draws no models.

### Changed
- **Faster solves.** Rails of up to 200k unknowns now get a direct solve rather than AMG,
  which on board meshes is 2–3× faster and exact, and makes later power-tree passes nearly
  free. The example board's power tree solves in 1.7 s instead of 3.0 s.
- **Faster electro-thermal runs on large boards.** Each iteration reuses the previous
  iteration's AMG hierarchy as the preconditioner rather than building a new one, because
  only the copper's resistivity has changed (89 s → 78 s for four iterations at 645k
  unknowns; 34 s with PARDISO).
- **AC sweeps about 5× faster with PARDISO.** The AC model is now factorized as a complex
  symmetric matrix by calling MKL directly, rather than as a real system twice its size
  through pypardiso. The matrix ordering is computed once per sweep rather than at every
  frequency, and the matrix is assembled with precomputed sparse products. On a 2M-unknown,
  68-port rail each field frequency takes 12.6 s instead of 69 s, so a full
  1 kHz–300 MHz sweep takes 24 min instead of about 2 h. Each factorization is still
  checked, and one that fails falls back to the real-equivalent PARDISO solve and then to
  SuperLU, as before.

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
