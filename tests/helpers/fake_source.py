"""A board source standing in for a design tool's editor, for window tests."""

from kipi.sources.base import BoardSource, Capabilities
from synthetic import make_board


class FakeSource(BoardSource):
    name = "Fake"
    description = "the fake editor"
    caps = Capabilities(reload=True, refill_zones=True, select_items=True)

    def __init__(self):
        self.loads = []
        self.selected = []
        self.focused = []

    def load(self, progress=None, refill_zones=False):
        self.loads.append(refill_zones)
        return make_board()

    def select(self, kind, item_id):
        self.selected.append((kind, item_id))

    def focus(self, kind, item_id, layer_id=None):
        self.focused.append((kind, item_id, layer_id))
