# Copyright (c) 2026 Tomachie LLC.
"""Undo and redo for the setup (#240): the setup after each of the last edits, kept as
copies. The board, the solve results and the views are not part of it."""

import copy
from typing import List, Optional

LIMIT = 25          # edits that can be undone


class SetupHistory:
    def __init__(self, limit: int = LIMIT):
        self.limit = limit
        self._states: List[object] = []     # oldest first; the last is the setup now
        self._redo: List[object] = []

    def reset(self, setup) -> None:
        """Start again from ``setup``, with nothing to undo or redo (a board was read)."""
        self._states = [copy.deepcopy(setup)]
        self._redo = []

    def record(self, setup) -> bool:
        """Keep ``setup`` as the newest state, if it differs from the one before; an edit
        drops what could be redone. True when it was kept."""
        if not self._states:
            self.reset(setup)
            return False
        if setup == self._states[-1]:
            return False
        self._states.append(copy.deepcopy(setup))
        del self._states[:-(self.limit + 1)]
        self._redo = []
        return True

    def amend(self, setup) -> None:
        """Replace the newest state with ``setup`` without dropping the redo steps: the
        panels may fill in a value (an AC rail that follows its VRM) as a setup is put
        back, which is not an edit."""
        if self._states:
            self._states[-1] = copy.deepcopy(setup)

    def can_undo(self) -> bool:
        return len(self._states) > 1

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> Optional[object]:
        """The setup before the last edit, as a copy to use, or None."""
        if not self.can_undo():
            return None
        self._redo.append(self._states.pop())
        return copy.deepcopy(self._states[-1])

    def redo(self) -> Optional[object]:
        """The setup the last undo went back from, as a copy to use, or None."""
        if not self._redo:
            return None
        self._states.append(self._redo.pop())
        return copy.deepcopy(self._states[-1])
