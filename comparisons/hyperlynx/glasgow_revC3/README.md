# Glasgow revC3: KiPI vs HyperLynx PI

DC drop cross-check for drewsum/KiPI#234.

- `glasgow_revC3_dc.bud`: the HyperLynx BoardSim session file (VRMs, `DC_CONN` series parts, DC sinks) loaded into both tools.
- `glasgow-kipi-setup.yml`: the KiPI setup from importing that BUD, with exact sink pins (see #251) and the thermal co-simulation on.
- `glasgow-kipi-setup-ldo.yml`: the same setup with U8, U36, U31 and U14 as KiPI linear regulators fed from `+5V` (their four `+5V` input-pin loads removed; quiescent current = BUD input current minus output current). Their ground pins are then solved, not held at 0 V. Their conversion loss (about 0.66 W in all) heats the board only when "Component heat" is on (#253).
- `glasgow-pi-crosscheck.html`: the pin-by-pin comparison page (self-contained; open it in a browser). It covers run 2: the LDO setup solved on the v1.0.2 branch (74f7a38) with component heat off. Run 1's version is in this file's history.

Board: [GlasgowEmbedded/glasgow](https://github.com/GlasgowEmbedded/glasgow) `hardware/boards/glasgow/revC3` at commit `70cb38c9b03471acd2eabb76c82308891bec2ca5`.
