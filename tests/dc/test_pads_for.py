"""Turning a pin into the pads that carry its current."""

import dataclasses

import pytest

from kipi.dc.solver import SolveError, pads_for
from tree_boards import board


def test_a_shapeless_pad_of_a_pin_with_copper_elsewhere_is_left_out():
    """A pin exported as several pads (a microphone's ground ring in four parts), one
    of them with no copper shape: the pads with copper carry the pin."""
    snap = board()
    u2_gnd = next(p for p in snap.pads if p.footprint == "U2" and p.net == "GND")
    snap.pads.append(dataclasses.replace(u2_gnd, id="u2_2b", shapes={}))
    assert [p.id for p in pads_for(snap, "GND", "U2")] == [u2_gnd.id]


def test_a_pin_with_no_copper_at_all_keeps_its_pads():
    snap = board()
    for i, p in enumerate(snap.pads):
        if p.footprint == "U2" and p.net == "GND":
            snap.pads[i] = dataclasses.replace(p, shapes={})
    assert len(pads_for(snap, "GND", "U2")) == 1      # the mesh then says what is wrong
    with pytest.raises(SolveError):
        pads_for(snap, "GND", "U9")
