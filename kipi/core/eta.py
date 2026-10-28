"""How long the running step has left (#224).

A step that reports how far it is (frequencies solved, nets meshed, CG converging) is
extrapolated from its rate so far. A step that cannot, a factorization above all, is
predicted from how long earlier ones of about the same size took on this computer: each
is recorded with its work (from the unknowns) in a small history file, and a new one is
scaled from the records nearest its size. With no record yet there is no estimate.

Qt-free: the GUI feeds :class:`Estimate` the progress signals and shows what it says.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("kipi")

#: Records kept per kind of step, the newest.
KEEP = 24
#: Records a prediction is scaled from, the nearest in size.
NEAREST = 3
#: Steps quicker than this are not recorded: their time is mostly overhead.
MIN_RECORD_S = 0.5
#: A rate needs this much of the step done, and this long, before it says anything.
MIN_FRACTION = 0.01
MIN_ELAPSED_S = 2.0


def factor_work(n: int, rhs: int = 0) -> float:
    """Rough work of factorizing ``n`` unknowns of a board mesh and solving ``rhs``
    right-hand sides with it: nested dissection of a planar mesh fills it in about
    n^1.5, and each solve sweeps the factor, about 5 n."""
    return float(n) ** 1.5 + 5.0 * n * rhs


class History:
    """Times of earlier steps on this computer: kind -> [(work, seconds)], in a JSON
    file. Reading or writing it never fails a solve."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path
        self._lock = threading.Lock()
        self._data: Optional[Dict[str, List[List[float]]]] = None

    @property
    def path(self) -> Path:
        if self._path is None:
            from kipi.launcher.paths import data_dir
            self._path = data_dir() / "timings.json"
        return self._path

    def _load(self) -> Dict[str, List[List[float]]]:
        if self._data is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = {k: [[float(w), float(s)] for w, s in v]
                              for k, v in data.items()} if isinstance(data, dict) else {}
            except (OSError, ValueError, TypeError):
                self._data = {}
        return self._data

    def predict(self, kind: str, work: float) -> Optional[float]:
        """Seconds a step of ``kind`` and ``work`` should take, or None with no record."""
        if work <= 0:
            return None
        with self._lock:
            recs = list(self._load().get(kind, ()))
        recs = [(w, s) for w, s in recs if w > 0 and s > 0]
        if not recs:
            return None
        recs.sort(key=lambda r: abs(math.log(r[0] / work)))
        k = min(NEAREST, len(recs))
        k -= 1 - k % 2                          # odd, so the median is one of them
        rates = sorted(s / w for w, s in recs[:k])
        return rates[len(rates) // 2] * work

    def record(self, kind: str, work: float, seconds: float):
        if work <= 0 or seconds < MIN_RECORD_S:
            return
        with self._lock:
            data = self._load()
            recs = data.setdefault(kind, [])
            recs.append([float(work), float(seconds)])
            del recs[:-KEEP]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as e:
                log.debug("Could not save step timings to %s: %s", self.path, e)


HISTORY = History()


@contextmanager
def predicted(progress, kind: str, work: float, upto: float = 1.0,
              history: Optional[History] = None):
    """Tell ``progress`` how long the step inside should take (it brings the stage's
    fraction to ``upto``), then record how long it took. ``kind`` is told apart by the
    caller (the solver backend, say), ``work`` from :func:`factor_work`. The context
    gives a dict whose ``"kind"`` the step can still change (a fallback backend)."""
    history = history or HISTORY
    seconds = history.predict(kind, work)
    if seconds is not None:
        progress.expect(seconds, upto)
    t0 = time.perf_counter()
    step = {"kind": kind}
    yield step
    history.record(step["kind"], work, time.perf_counter() - t0)


class Estimate:
    """Time left in the current stage, from its progress signals.

    ``fraction`` is what the stage reports (negative: not known); ``expect`` says the
    fraction should reach ``upto`` in about ``seconds``. Times are ``time.monotonic()``,
    or whatever clock the caller passes."""

    def __init__(self, now: Optional[float] = None):
        self.stage(now)

    def stage(self, now: Optional[float] = None):
        self.t0 = time.monotonic() if now is None else now
        self.f = 0.0            # the last known fraction, and when it came
        self.t_f = self.t0
        self.hint = None        # (when, seconds, upto, fraction then)

    def fraction(self, f: float, now: Optional[float] = None):
        if f < 0:
            return
        now = time.monotonic() if now is None else now
        self.f, self.t_f = min(float(f), 1.0), now
        if self.hint is not None and self.f >= self.hint[2]:
            self.hint = None

    def expect(self, seconds: float, upto: float = 1.0, now: Optional[float] = None):
        now = time.monotonic() if now is None else now
        upto = min(max(float(upto), 1e-6), 1.0)
        if seconds > 0 and upto > self.f:
            self.hint = (now, float(seconds), upto, self.f)

    def remaining(self, now: Optional[float] = None) -> Optional[float]:
        """Seconds left, or None when there is nothing to go on (or the step has run
        past what was expected of it)."""
        now = time.monotonic() if now is None else now
        hint = self.hint                # set from the solve's thread
        if hint is not None:
            t_h, seconds, upto, f_h = hint
            left = t_h + seconds - now
            if left < 0:
                return None
            # The rest of the stage at the rate the hint implies.
            return left + seconds * (1.0 - upto) / (upto - f_h)
        f, ran = self.f, self.t_f - self.t0
        if f >= 1.0 or f < MIN_FRACTION or now - self.t0 < MIN_ELAPSED_S or ran <= 0:
            return None
        return max(ran * (1.0 - f) / f - (now - self.t_f), 0.0)


def left_text(seconds: float) -> str:
    """"about 40 s left", "about 3 min left", "about 1 h 20 min left"."""
    if seconds < 55:
        return f"about {max(10, int(round(seconds / 10.0)) * 10)} s left"
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return f"about {minutes} min left"
    h, m = divmod(minutes, 60)
    return f"about {h} h {m} min left" if m else f"about {h} h left"
