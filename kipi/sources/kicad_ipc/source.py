"""The running KiCad as a :class:`~kipi.sources.base.BoardSource`, and the KiCad installed
on this computer as a :class:`~kipi.sources.base.InstalledTool`."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter
from kipi.sources.base import BoardSource, Capabilities, InstalledTool, ModelProvider
from kipi.sources.kicad_ipc import colors, model_guess, models3d

#: KiCad actions that bring the view to the selection, tried in turn: centre on it (no
#: zoom change), else zoom to fit it.
CENTRE_ACTIONS = ("common.Control.centerSelection", "common.Control.zoomFitSelection")


class KiCadLiveSource(BoardSource):
    """The board open in the running KiCad PCB editor, through the IPC API.

    ``kicad`` and ``board`` are an existing connection (from
    :func:`~kipi.sources.kicad_ipc.extract.connect`); without one, the first
    :meth:`load` connects.
    """
    name = "KiCad"
    description = "the running KiCad PCB editor"
    caps = Capabilities(reload=True, refill_zones=True, select_items=True)

    def __init__(self, kicad=None, board=None):
        self.kicad = kicad
        self.board = board

    def load(self, progress: ProgressReporter = NULL,
             refill_zones: bool = False) -> BoardSnapshot:
        from kipi.sources.kicad_ipc.extract import connect, extract_board, require_single_kicad
        require_single_kicad()     # a second KiCad may have been opened since the last load
        if self.board is None:
            self.kicad, self.board = connect(progress=progress)
        snap = extract_board(self.board, refill_zones=refill_zones, progress=progress)
        try:
            snap.kicad_version = str(self.kicad.get_version())
        except Exception:  # noqa: BLE001
            pass
        return snap

    def can_select(self) -> bool:
        return self.board is not None

    def select(self, kind: str, item_id: str) -> None:
        from kipy.proto.common.types import KIID

        class _Ref:  # add_to_selection only needs an object with an ``id``
            def __init__(self, value):
                self.id = KIID(value=value)

        self.board.clear_selection()
        self.board.add_to_selection([_Ref(item_id)])

    def focus(self, kind: str, item_id: str, layer_id: Optional[int] = None) -> None:
        """The API has no call to move the view to a point, so the item is selected and
        the view centred on the selection (#208). A KiCad without that action zooms to
        the selection instead."""
        self.select(kind, item_id)
        if layer_id is not None:
            try:                    # brings the picked layer's copper to the front
                self.board.set_active_layer(layer_id)
            except Exception:  # noqa: BLE001 - the view still moves
                pass
        if self.kicad is None:
            return
        from kipy.proto.common.commands.editor_commands_pb2 import RunActionStatus
        for action in CENTRE_ACTIONS:
            if self.kicad.run_action(action).status == RunActionStatus.RAS_OK:
                return


class KiCadCliModels(ModelProvider):
    """Part models exported from the saved board file by kicad-cli."""
    label = "kicad-cli"

    def __init__(self, cli: List[str], board: Path):
        self.cli = cli
        self.board = board

    def load(self, refs: Iterable[str],
             progress: ProgressReporter = NULL) -> Dict[str, Optional[object]]:
        return models3d.load_models(self.cli, self.board, refs, progress)


class InstalledKiCad(InstalledTool):
    """KiCad's color theme (read from its settings folder) and part models (through its
    kicad-cli). ``live`` is the running KiCad, when there is one: it knows where its own
    kicad-cli is."""
    name = "KiCad"
    offers_part_models = True

    def __init__(self, live: Optional[KiCadLiveSource] = None):
        self._live = live
        self._cli: Optional[List[str]] = None
        self._cli_looked = False          # kicad-cli is looked for once per session

    def layer_colors(self, snap: BoardSnapshot):
        return colors.copper_theme(snap.kicad_version)

    def _find_cli(self) -> Optional[List[str]]:
        if not self._cli_looked:
            self._cli_looked = True
            self._cli = models3d.find_kicad_cli(self._live.kicad if self._live else None)
        return self._cli

    def part_models(self, snap: BoardSnapshot) -> Tuple[Optional[ModelProvider], str]:
        if self._find_cli() is None:
            return None, ("3D models are read with KiCad's kicad-cli, which KiPI could not "
                          f"find; set {models3d.CLI_ENV} to its path to show them")
        board = models3d.board_file(snap)
        if board is None:
            return None, (f"3D models: the board file {snap.board_name} was not found next "
                          "to the project, so no models are shown. Guess models picks them "
                          "from KiCad's library by footprint name.")
        return KiCadCliModels(self._cli, board), ""

    def guessed_models(self, snap: BoardSnapshot) -> Tuple[Optional[ModelProvider], str]:
        if self._find_cli() is None:
            return None, ("3D models are read with KiCad's kicad-cli, which KiPI could not "
                          f"find; set {models3d.CLI_ENV} to its path to show them")
        guessed = model_guess.guessed_models(self._cli, snap)
        if guessed is None:
            return None, ("3D models: KiCad's 3D model library was not found; set "
                          "KICAD10_3DMODEL_DIR (or your KiCad version's) to its folder")
        return guessed, ""


class KiCadPartModels(InstalledKiCad):
    """For KiPI on its own: KiPI's colors, and KiCad's part models when KiCad is installed
    on this computer (the 3D view offers none when it is not)."""

    @property
    def offers_part_models(self) -> bool:
        return self._find_cli() is not None

    def layer_colors(self, snap: BoardSnapshot):
        return InstalledTool.layer_colors(self, snap)
