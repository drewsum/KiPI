"""Coupled Noise mode's 3D markers (#69): each victim's driver and receiver pins."""

import os

import numpy as np
import pytest

from kipi.dc.pi.model import PinRef
from kipi.noise.markers import noise_markers
from validation_boards import open_board


def test_each_victim_marks_its_driver_and_receiver():
    snap, setup = open_board("coupled_noise")
    ms = [m for m in noise_markers(snap, setup, ["VIN", "GND"]) if m.kind == "victim"]
    assert sorted(m.name for m in ms) == sorted(
        f"{n} {role}" for n in ("SIG_SW", "SIG_LOOP", "SIG_BURIED")
        for role in ("driver", "receiver"))
    drv = next(m for m in ms if m.name == "SIG_LOOP driver")
    pad = next(p for p in snap.pads if p.footprint == "U5" and p.number == "1")
    assert np.allclose(drv.power[0][:2], pad.position)
    # Selecting the victim marks both of its ends.
    assert sum(m.is_item("victim", "SIG_LOOP") for m in ms) == 2


def test_ends_not_set_are_the_guessed_ones():
    snap, setup = open_board("coupled_noise")
    setup.victims[0].driver = setup.victims[0].receiver = None
    ms = [m for m in noise_markers(snap, setup, []) if m.item == setup.victims[0].name]
    assert len(ms) == 2
    setup.victims[0].driver = PinRef("U9", "SIG_SW", ["1"])      # not on the board
    ms = [m for m in noise_markers(snap, setup, []) if m.item == setup.victims[0].name]
    assert [m.name for m in ms] == [f"{setup.victims[0].name} receiver"]


def test_the_panel_names_the_selected_victim():
    pytest.importorskip("qtpy")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from kipi.noise.gui.panel import NoisePanel
    snap, setup = open_board("coupled_noise")
    p = NoisePanel()
    p.set_board(snap, setup, "test")
    got = []
    p.part_selected.connect(lambda *a: got.append(a))
    assert p.select_part("victim", "SIG_LOOP")
    assert got[-1][:2] == ("victim", "SIG_LOOP")
    assert p.tabs.currentWidget() is p.victim_list
    p.close()
