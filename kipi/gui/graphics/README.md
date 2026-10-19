# Tooltip graphics

Drawings shown under tooltips that explain a visual engineering idea (#116): a
capacitor's ESL, a VRM's load line, the DC bias curve. Every tooltip drawing lives here;
`kipi/gui/tooltip_art.py` renders them and `art_tip(text, name, *highlight)` makes the
tooltip.

When you write a tooltip for a field whose meaning is easier to see than to read (a curve,
a circuit, a cross-section), add a drawing for it here.

Conventions:

- SVG, about 280 x 140 to 190 px (`width`/`height` on the `<svg>` element, matched by
  `viewBox`), `font-family="sans-serif"`, `font-size="11"`.
- Colours are tokens filled from the window's palette, never fixed colours, so a drawing
  matches the light or dark GUI:
  - `{bg}` the background, a shade darker than the window: start with
    `<rect ... rx="8" fill="{bg}"/>` over the whole drawing;
  - `{fg}` text and lines, `{muted}` axes and guides, `{grid}` faint fills,
    `{accent}` emphasis, `{warn}` a warning tint.
- Any other token (`{c}`, `{ll}`, `{range}`) names a part a tooltip can point at: drawn in
  `{accent}` when the tooltip passes its name in `highlight`, else in `{fg}`. One drawing
  can serve several fields that way.
- Each drawing must be used by a tooltip (`tests/gui/test_tooltip_art.py` checks).
