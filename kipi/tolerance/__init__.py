"""Tolerance stack-up (#167): each load's DC drop, its VRM's DC accuracy, the ripple at its
probe (Coupled Noise) and its transient (AC) added up against the load's DC, AC and total
tolerances. The results are too large to hold at once, so the setup keeps a few numbers per
load from each, accepted as the solves are run or their results opened."""
