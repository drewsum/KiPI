"""The time left in a step (#224): from its rate, or predicted from earlier steps."""

import json

import pytest

from kipi.core import eta
from kipi.core.progress import ProgressReporter


def test_rate_of_a_reporting_stage():
    e = eta.Estimate(now=0.0)
    e.fraction(0.25, now=10.0)
    assert e.remaining(now=10.0) == pytest.approx(30.0)
    assert e.remaining(now=20.0) == pytest.approx(20.0)     # counts down between reports
    assert e.remaining(now=50.0) == 0.0
    e.stage(now=60.0)                                       # a new stage knows nothing yet
    assert e.remaining(now=61.0) is None


def test_too_early_to_say():
    e = eta.Estimate(now=0.0)
    e.fraction(0.005, now=1.0)
    assert e.remaining(now=1.5) is None                     # too little done
    e.fraction(0.5, now=1.0)
    assert e.remaining(now=1.5) is None                     # too soon
    assert e.remaining(now=2.5) is not None
    e.fraction(-1.0, now=3.0)                               # "not known" keeps what was
    assert e.f == 0.5
    e.fraction(1.0, now=4.0)
    assert e.remaining(now=4.0) is None                     # done


def test_a_prediction_and_the_rest_of_the_stage():
    e = eta.Estimate(now=0.0)
    e.expect(100.0, upto=0.25, now=0.0)    # the first of four frequencies, say
    assert e.remaining(now=0.0) == pytest.approx(400.0)
    assert e.remaining(now=40.0) == pytest.approx(360.0)
    assert e.remaining(now=101.0) is None                   # longer than expected
    e.fraction(0.25, now=120.0)                             # then its own rate
    assert e.hint is None
    assert e.remaining(now=120.0) == pytest.approx(360.0)


def test_history_scales_from_the_nearest_sizes(tmp_path):
    h = eta.History(tmp_path / "t.json")
    assert h.predict("f", 1e6) is None
    h.record("f", 1e6, 10.0)
    h.record("f", 1e9, 5000.0)
    h.record("f", 1e6, 0.1)                     # too quick to tell anything: not kept
    assert h.predict("f", 2e6) == pytest.approx(20.0)
    assert h.predict("other", 1e6) is None
    # Saved for the next session.
    assert json.loads((tmp_path / "t.json").read_text())["f"] == [[1e6, 10.0], [1e9, 5000.0]]
    assert eta.History(tmp_path / "t.json").predict("f", 1e9) == pytest.approx(5000.0, rel=0.5)


def test_history_keeps_the_newest(tmp_path):
    h = eta.History(tmp_path / "t.json")
    for i in range(eta.KEEP + 5):
        h.record("f", 1.0 + i, 1.0)
    assert len(h._load()["f"]) == eta.KEEP


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"f": [[1]]}'])
def test_a_broken_history_is_ignored(tmp_path, content):
    (tmp_path / "t.json").write_text(content)
    h = eta.History(tmp_path / "t.json")
    assert h.predict("f", 1.0) is None
    h.record("f", 1.0, 1.0)
    assert h.predict("f", 1.0) == pytest.approx(1.0)


def test_an_unwritable_history_does_not_fail(tmp_path):
    (tmp_path / "dir").write_text("a file where the folder should be")
    h = eta.History(tmp_path / "dir" / "t.json")
    h.record("f", 1.0, 1.0)                     # logged, not raised
    assert h.predict("f", 1.0) == pytest.approx(1.0)


class _Expect(ProgressReporter):
    def __init__(self):
        super().__init__()
        self.said = []

    def expect(self, seconds, upto=1.0):
        self.said.append((seconds, upto))


def test_predicted_tells_and_records(tmp_path, monkeypatch):
    h = eta.History(tmp_path / "t.json")
    t = iter([0.0, 2.0])
    monkeypatch.setattr(eta.time, "perf_counter", lambda: next(t))
    p = _Expect()
    with eta.predicted(p, "a", 100.0, history=h) as step:
        step["kind"] = "b"                      # the backend that did it
    assert p.said == []                         # nothing to go on the first time
    assert h.predict("b", 200.0) == pytest.approx(4.0)
    with pytest.raises(RuntimeError):           # a step that fails is not recorded
        with eta.predicted(p, "b", 50.0, upto=0.5, history=h):
            raise RuntimeError
    assert p.said == [(pytest.approx(1.0), 0.5)]
    assert len(h._load()["b"]) == 1


def test_left_text():
    assert eta.left_text(3) == "about 10 s left"
    assert eta.left_text(42) == "about 40 s left"
    assert eta.left_text(170) == "about 3 min left"
    assert eta.left_text(3600) == "about 1 h left"
    assert eta.left_text(4800) == "about 1 h 20 min left"


def test_factor_work_grows_with_size_and_right_hand_sides():
    assert eta.factor_work(10_000) == pytest.approx(1e6)
    assert eta.factor_work(10_000, 100) > eta.factor_work(10_000)
