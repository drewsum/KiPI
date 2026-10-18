"""What the application shell needs from wherever a board comes from.

Two things are kept apart:

* a :class:`BoardSource` is where the board is read from (the running KiCad, a file). It
  reads the board and, if it can, re-reads it, refills its zones or selects an item in
  the editor it came from.
* an :class:`InstalledTool` is a design tool on this computer that KiPI borrows from:
  the layer colors of its theme and the 3D models of the parts. These do not depend on
  how the board was read; a snapshot opened from disk still shows KiCad's colors and
  models when KiCad is installed.

The window only sees these interfaces, never a concrete source (``tests/core/test_layering.py``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter
from kipi.core.viz.layer_colors import DEFAULT_COPPER, RGB


@dataclass(frozen=True)
class Capabilities:
    """What a source can do besides reading the board once."""
    reload: bool = False          # read the board again, e.g. after it was edited
    refill_zones: bool = False    # refill the zones before reading
    select_items: bool = False    # select a picked pad or via in the editor


class BoardSource(ABC):
    name: str = ""                # short, for buttons: "Load from <name>"
    description: str = ""         # for tooltips: "Re-read the board from <description>"
    caps: Capabilities = Capabilities()

    @abstractmethod
    def load(self, progress: ProgressReporter = NULL,
             refill_zones: bool = False) -> BoardSnapshot:
        """Read the board. Runs on a worker thread; may raise to report a failure."""

    def files(self) -> List[Path]:
        """The files the board is read from; when one changes on disk, the board has
        changed (the window offers to reload it). Empty for a live source."""
        return []

    def can_select(self) -> bool:
        """Whether :meth:`select` would work now (a source may need to connect first)."""
        return self.caps.select_items

    def select(self, kind: str, item_id: str) -> None:
        """Select the ``kind`` ("pad" or "via") with ``item_id`` in the editor."""
        raise NotImplementedError(f"{self.name or 'This source'} cannot select items")


class ModelProvider(ABC):
    label: str = ""               # what reads the models, for the log ("kicad-cli")

    @abstractmethod
    def load(self, refs: Iterable[str],
             progress: ProgressReporter = NULL) -> Dict[str, Optional[object]]:
        """Each part in ``refs`` with its model mesh, or None when it has none. Runs on
        a worker thread."""


class InstalledTool:
    """No design tool to borrow from: KiPI's own colors and no part models."""
    name: str = ""
    offers_part_models: bool = False   # False hides the part models from the view

    def layer_colors(self, snap: BoardSnapshot) -> Tuple[Dict[str, RGB], str]:
        """Copper colors by theme key, and where they came from (for the log). Never
        raises."""
        return dict(DEFAULT_COPPER), "KiPI default colors"

    def part_models(self, snap: BoardSnapshot) -> Tuple[Optional[ModelProvider], str]:
        """What reads the part models of ``snap``, or None and why not ("" when part
        models are not offered at all)."""
        return None, ""

    def guessed_models(self, snap: BoardSnapshot) -> Tuple[Optional[ModelProvider], str]:
        """What guesses part models for ``snap`` from a model library, when the user asks
        for it, or None and why not."""
        return None, "No 3D model library to guess part models from"
