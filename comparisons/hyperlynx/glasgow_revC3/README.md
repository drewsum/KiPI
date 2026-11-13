# Glasgow revC3: KiPI vs HyperLynx PI

DC drop and AC impedance cross-check for drewsum/KiPI#234.

- `glasgow_revC3_dc.bud`: the HyperLynx BoardSim session file (VRMs, `DC_CONN` series parts, DC sinks) loaded into both tools.
- `glasgow-kipi-setup.yml`: the KiPI setup from importing that BUD, with exact sink pins (see #251) and the thermal co-simulation on.
- `glasgow-kipi-setup-ldo.yml`: the same setup with U8, U36, U31 and U14 as KiPI linear regulators fed from `+5V` (their four `+5V` input-pin loads removed; quiescent current = BUD input current minus output current). Their ground pins are then solved, not held at 0 V. Their conversion loss (about 0.66 W in all) heats the board only when "Component heat" is on (#253).
- The pin-by-pin comparison page is [`docs/hyperlynx-comparison.html`](../../../docs/hyperlynx-comparison.html), hosted at <https://drewsum.github.io/KiPI/hyperlynx-comparison.html>. It covers run 2: the LDO setup solved on the v1.0.2 branch (74f7a38) with component heat off. Run 1's version is in the history of `comparisons/hyperlynx/glasgow_revC3/glasgow-pi-crosscheck.html`.

## AC impedance (U30 on +1V2 and +3V3)

- `glasgow_revC3_ac.bud`: the DC session file plus the capacitor groups (`DCCGROUPS`, `DCCAPACITORS`): `C100n_0402`, `C4u7_0603` and `C150u_POLY`, one per part number. HyperLynx keeps only the group names and members; each group's C, ESR and ESL are entered in HyperLynx.
- `glasgow-kipi-setup-ldo.yml` carries the matching KiPI models for +3V3 and +1V2: `C100n_0402` (100 nF, 30 mΩ, 0.40 nH) and `C4u7_0603` (4.7 µF, 8 mΩ, 0.58 nH). It also has probes at U30 (+1V2 balls D4/D8/H4/H8; +3V3 balls C10/D6/F8/H6/L11; against U30's GND balls) and U1, with DC bias, tolerance, aging and temperature derating off.
- `ac/hyperlynx_U30_1V2.z1p`, `ac/hyperlynx_U30_3V3.z1p`: HyperLynx PI (Hybrid Solver VX.2.8) Z-parameters at U30. The header lines naming the machine, user and directory are removed.
- `ac/kipi_U30_1V2.z1p`, `ac/kipi_U30_3V3.z1p`, `ac/kipi_U1_3V3.z1p`: KiPI's, from the LDO setup at its own sweep settings (1 kHz to 300 MHz).
- `ac/glasgow_U30_Z_compare.csv`: both at HyperLynx's 250 frequencies (KiPI interpolated), and `ac/glasgow_U30_Z_hyperlynx_vs_kipi.png` the plot.

Board: [GlasgowEmbedded/glasgow](https://github.com/GlasgowEmbedded/glasgow) `hardware/boards/glasgow/revC3` at commit `70cb38c9b03471acd2eabb76c82308891bec2ca5`.
