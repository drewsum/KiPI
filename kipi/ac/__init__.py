"""AC power integrity: the impedance seen at probe points on the power distribution
network, swept over frequency.

The setup (:mod:`.model`: capacitor models, probes, sweep settings) lives in the same file
as the DC power tree, and shares its VRMs and series links. :mod:`.solver` field-solves
one rail and its ground and terminates the ports with the parts at every frequency;
:mod:`.gui` holds the AC setup panel, the impedance plot and the target calculator. It
builds on the shared board model, meshing and solvers in :mod:`kipi.core`.
"""
