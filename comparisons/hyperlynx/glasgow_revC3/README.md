# Glasgow revC3: KiPI vs HyperLynx PI

DC drop and AC impedance cross-check for drewsum/KiPI#234.

- `glasgow_revC3_dc.bud`: the HyperLynx BoardSim session file (VRMs, `DC_CONN` series parts, DC sinks) loaded into both tools.
- `glasgow-kipi-setup.yml`: the KiPI setup from importing that BUD, with exact sink pins (see #251) and the thermal co-simulation on.
- `glasgow-kipi-setup-ldo.yml`: the same setup with U8, U36, U31 and U14 as KiPI linear regulators fed from `+5V` (their four `+5V` input-pin loads removed; quiescent current = BUD input current minus output current). Their ground pins are then solved, not held at 0 V. Their conversion loss (about 0.66 W in all) heats the board only when "Component heat" is on (#253).
- The pin-by-pin comparison page is [`docs/hyperlynx-comparison.html`](../../../docs/hyperlynx-comparison.html), hosted at <https://drewsum.github.io/KiPI/hyperlynx-comparison.html>. It covers run 2: the LDO setup solved on the v1.0.2 branch (74f7a38) with component heat off. Run 1's version is in the history of `comparisons/hyperlynx/glasgow_revC3/glasgow-pi-crosscheck.html`.

## AC impedance (U30 on +1V2 and +3V3)

- `glasgow_revC3_ac.bud`: the DC session file plus the capacitor groups (`DCCGROUPS`, `DCCAPACITORS`): `C100n_0402`, `C4u7_0603` and `C150u_POLY`, one per part number. HyperLynx keeps only the group names and members; each group's C, ESR and ESL are entered in HyperLynx.
- `glasgow-kipi-setup-ldo.yml` carries the matching KiPI models for +3V3 and +1V2: `C100n_0402` (100 nF, 30 mΩ, 0.40 nH) and `C4u7_0603` (4.7 µF, 8 mΩ, 0.58 nH). It also has probes at U30 (+1V2 balls D4/D8/H4/H8; +3V3 balls C10/D6/F8/H6/L11; against U30's GND balls) and U1, with DC bias, tolerance, aging and temperature derating off.
- `ac/hyperlynx_U30_1V2.z1p`, `ac/hyperlynx_U30_3V3.z1p`: HyperLynx PI (Hybrid Solver VX.2.8) Z-parameters at U30. The header lines naming the machine, user and directory are removed. +3V3 is from the `hyp-export/` board (U1's thermal vias present); +1V2 is from the first export. `ac/hyperlynx_U30_3V3_run1_U1_vias_missing.z1p` is the first +3V3 run, 5.5 µF short.
- `ac/kipi_U30_1V2.z1p`, `ac/kipi_U30_3V3.z1p`, `ac/kipi_U1_3V3.z1p`: KiPI's, from the LDO setup at its own sweep settings (1 kHz to 300 MHz).
- `ac/glasgow_U30_Z_compare.csv`: both at HyperLynx's 250 frequencies (KiPI interpolated), and `ac/glasgow_U30_Z_hyperlynx_vs_kipi.png` the plot.

## Exporting the board to HyperLynx (`hyp-export/`)

KiCad's `.hyp` export loses plated-through pads that share a pin number within one footprint (HyperLynx names pins `refdes.pin`). On revC3 that drops U1's 25 thermal vias (all pad 57), U8's and U36's (pad 7) and the mounting holes' stitching vias. C1–C7, C9 and C10 return to GND only through U1's thermal vias, so in HyperLynx they float and +3V3 is short 4.7 µF + 8 × 100 nF (12.3 µF seen against 17.8 µF).

- `hyp-export/glasgow_revC3_hypfix.kicad_pcb`: the revC3 board with every repeated plated pad turned into a GND board via at the same place, size and drill, and the second copy of each exposed pad into a copper zone of its shape. The copper is unchanged; only the pin names become unique. J1's slotted shield tabs (`/SHLD`, not simulated) are left as they are. Export the `.hyp` from this file (File → Export → Hyperlynx).
- `hyp-export/unique_pins.py`: the script that made it, with KiCad's `pcbnew` module: `python3 unique_pins.py glasgow.kicad_pcb glasgow_revC3_hypfix.kicad_pcb`.

## DC drop from the fixed export (`dc/`)

- `dc/hyperlynx_dc_fixed_export.txt`: HyperLynx PI DC drop for all six analyses, solved on the `.hyp` exported from `hyp-export/glasgow_revC3_hypfix.kicad_pcb` (U1, U8, U36 thermal vias and the mounting-hole vias present). The header lines naming the machine, user and directory are removed, and the all-zero GND "Other vias" table of the VIO_AUX analysis is left out.
- Against the first export, every plane and ground pin moves by at most 0.1 mV (HyperLynx's display step). The series parts' drops are higher than in the first run and no longer scale by one factor (×1.023 before; ×1.028 to ×1.046 now, R31/R32 highest), which moves the `+5V` pins down 0.7 to 0.9 mV and the PLL pins down 2.0 to 2.2 mV. The sink pins also differ: U1's +3V3 ground sink is on ten GND pins (was U1.56/U1.57), and U8's ground is U8.7 only.
- `dc/hyperlynx_temp_*.png`: HyperLynx's copper temperature for that run, one map per layer (24.0 to 31.6 °C). HyperLynx's series-part resistances follow it: each part's factor matches copper's temperature coefficient (0.393 %/°C from 20 °C) at the map's temperature under the part, from about 27 °C at FB1 to about 31.5 °C at R31/R32 beside the FPGA.

Board: [GlasgowEmbedded/glasgow](https://github.com/GlasgowEmbedded/glasgow) `hardware/boards/glasgow/revC3` at commit `70cb38c9b03471acd2eabb76c82308891bec2ca5`.
