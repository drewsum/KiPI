"""The AC solve logs each field-solve frequency as it starts it."""

import logging
import re

from ac_boards import plane_pair, plane_setup
from kipi.ac.solver import solve_ac
from kipi.ac.solver.sweep import fmt_hz
from kipi.core.progress import ProgressReporter


class Recorder(ProgressReporter):
    def __init__(self):
        super().__init__()
        self.lines = []

    def message(self, text, level=logging.INFO):
        self.lines.append(text)


def test_fmt_hz():
    assert fmt_hz(999.0) == "999 Hz"
    assert fmt_hz(1e3) == "1 kHz"
    assert fmt_hz(12.3456e6) == "12.35 MHz"
    assert fmt_hz(1.5e9) == "1.5 GHz"


def test_each_field_frequency_is_logged_before_it_is_solved():
    snap = plane_pair()
    setup = plane_setup(snap, f_start_hz=1e6, f_stop_hz=100e6, points_per_decade=40,
                        field_points_per_decade=2)
    rec = Recorder()
    result = solve_ac(snap, setup, "VRM1", "GND", rec)
    n = len(result.field_freqs)
    logged = [l for l in rec.lines if re.match(r"\s+\[\d+/\d+\] solving ", l)]
    assert logged == [f"  [{k + 1}/{n}] solving {fmt_hz(f)}"
                      for k, f in enumerate(result.field_freqs)]
