# Glasgow revC3: KiPI vs HyperLynx PI

DC drop cross-check for drewsum/KiPI#234.

- `glasgow_revC3_dc.bud`: the HyperLynx BoardSim session file (VRMs, `DC_CONN` series parts, DC sinks) loaded into both tools.
- `glasgow-kipi-setup.yml`: the KiPI setup from importing that BUD, with exact sink pins (see #251) and the thermal co-simulation on.
- `glasgow-pi-crosscheck.html`: the pin-by-pin comparison page (self-contained; open it in a browser).

Board: [GlasgowEmbedded/glasgow](https://github.com/GlasgowEmbedded/glasgow) `hardware/boards/glasgow/revC3` at commit `70cb38c9b03471acd2eabb76c82308891bec2ca5`.
