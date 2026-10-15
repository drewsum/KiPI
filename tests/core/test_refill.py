"""Waiting for a KiCad zone refill, against a fake kipy board."""
import pytest

pytest.importorskip("kipy")

from kipy.errors import ApiError  # noqa: E402
from kipy.proto.common.envelope_pb2 import ApiStatusCode  # noqa: E402

from kipi.core.kicad_io.extract import ExtractionError, refill_and_wait  # noqa: E402


class FakeBoard:
    """Answers each ``get_items`` with the next scripted state: ok, busy or timeout."""

    def __init__(self, states):
        self.states = list(states)
        self.refilled = False
        self.polls = 0

    def refill_zones(self, block=True, **_):
        assert not block
        self.refilled = True

    def get_items(self, _types):
        self.polls += 1
        state = self.states.pop(0) if self.states else "ok"
        if state == "busy":
            raise ApiError("busy", code=ApiStatusCode.AS_BUSY)
        if state == "timeout":
            raise ConnectionError("timed out")
        return []


def test_waits_past_idle_window_until_fill_done():
    # Idle before the queued fill starts (what fooled the Ping-based wait), then busy.
    b = FakeBoard(["ok", "ok", "busy", "timeout", "busy", "ok"])
    refill_and_wait(b, poll_s=0, grace_s=60)
    assert b.refilled and b.polls == 6


class FakeClock:
    """Stands in for ``time`` in extract, so the grace period does not depend on how
    long a sleep really takes on a loaded CI runner."""

    def __init__(self):
        self.now = 0.0

    def sleep(self, s):
        self.now += s

    def perf_counter(self):
        return self.now


def test_quick_fill_returns_after_grace(monkeypatch):
    monkeypatch.setattr("kipi.core.kicad_io.extract.time", FakeClock())
    b = FakeBoard([])
    refill_and_wait(b, poll_s=1, grace_s=5)
    assert b.polls == 5


def test_other_api_errors_propagate():
    class Broken(FakeBoard):
        def get_items(self, _types):
            raise ApiError("no board", code=ApiStatusCode.AS_BAD_REQUEST)

    with pytest.raises(ApiError):
        refill_and_wait(Broken([]), poll_s=0)


def test_gives_up_after_timeout():
    class Stuck(FakeBoard):
        def get_items(self, _types):
            raise ApiError("busy", code=ApiStatusCode.AS_BUSY)

    with pytest.raises(ExtractionError):
        refill_and_wait(Stuck([]), poll_s=0.001, timeout_s=0.02)
