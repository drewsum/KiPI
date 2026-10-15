"""Copy an edited stackup's thicknesses back to the board in KiCad (#61).

The IPC API has an UpdateBoardStackup command, but KiCad 9 and 10 do not carry it out
yet. :func:`write_stackup` tries it, so a KiCad that does will take the thicknesses
directly; otherwise it raises :class:`StackupWriteUnsupported` and the GUI lists the
values to enter in Board Setup instead (:func:`manual_rows`).
"""

from __future__ import annotations

from typing import List, Tuple

from kipi.core.model.board import CopperLayer, Stackup
from kipi.core.model.stackup import stack_order


class StackupWriteUnsupported(RuntimeError):
    """KiCad cannot change the stackup through its API."""


def _nm(mm: float) -> int:
    return int(round(mm * 1e6))


def _set_dielectric(sl, t_mm: float) -> None:
    """Set a dielectric stackup entry to ``t_mm``, sharing it among its sublayers in the
    proportions they have now."""
    subs = list(sl.dielectric.layer) if sl.HasField("dielectric") else []
    if not subs:
        sl.thickness.value_nm = _nm(t_mm)
        return
    old = [s.thickness.value_nm for s in subs]
    total_nm = _nm(t_mm)
    if sum(old) <= 0:
        old = [1] * len(subs)
    left = total_nm
    for i, s in enumerate(subs):
        part = left if i == len(subs) - 1 else int(round(total_nm * old[i] / sum(old)))
        s.thickness.value_nm = part
        left -= part
    sl.thickness.value_nm = total_nm


def updated_stackup_proto(kicad_stackup, stackup: Stackup):
    """A copy of the board's stackup message with ``stackup``'s thicknesses."""
    from kipy.proto.board import board_pb2

    proto = board_pb2.BoardStackup()
    proto.CopyFrom(kicad_stackup)
    reports_enabled = any(sl.enabled for sl in proto.layers)
    copper = {c.id: c.thickness_mm for c in stackup.copper}
    dielectrics = [e.thickness_mm for e in stack_order(stackup)
                   if not isinstance(e, CopperLayer)]
    entries = [sl for sl in proto.layers if sl.enabled or not reports_enabled]
    kicad_diel = [sl for sl in entries
                  if sl.type == board_pb2.BoardStackupLayerType.BSLT_DIELECTRIC]
    if len(kicad_diel) != len(dielectrics):
        raise ValueError(f"the board in KiCad has {len(kicad_diel)} dielectric layers, "
                         f"KiPI's stackup {len(dielectrics)}; load the board again")
    for sl in entries:
        if sl.type == board_pb2.BoardStackupLayerType.BSLT_COPPER and sl.layer in copper:
            sl.thickness.value_nm = _nm(copper[sl.layer])
    for sl, t in zip(kicad_diel, dielectrics):
        _set_dielectric(sl, t)
    return proto


def write_stackup(board, stackup: Stackup) -> None:
    """Set the thicknesses of the board's stackup in KiCad to ``stackup``'s."""
    from kipy.errors import ApiError
    from kipy.proto.board import board_commands_pb2
    from kipy.proto.common import ApiStatusCode

    current = board._kicad.send(_get_cmd(board), board_commands_pb2.BoardStackupResponse)
    cmd = board_commands_pb2.UpdateBoardStackup()
    cmd.board.CopyFrom(board._doc)
    cmd.stackup.CopyFrom(updated_stackup_proto(current.stackup, stackup))
    try:
        board._kicad.send(cmd, board_commands_pb2.BoardStackupResponse)
    except ApiError as e:
        if e.code in (ApiStatusCode.AS_UNHANDLED, ApiStatusCode.AS_UNIMPLEMENTED):
            raise StackupWriteUnsupported(
                "this version of KiCad cannot change the stackup through its API") from e
        raise


def _get_cmd(board):
    from kipy.proto.board import board_commands_pb2
    cmd = board_commands_pb2.GetBoardStackup()
    cmd.board.CopyFrom(board._doc)
    return cmd


def manual_rows(stackup: Stackup) -> List[Tuple[str, str]]:
    """``(layer, thickness)`` rows, top to bottom, to type into KiCad's Board Setup >
    Board Stackup > Physical Stackup."""
    names = {c.id: c.name for c in stackup.copper}
    rows = []
    for e in stack_order(stackup):
        if isinstance(e, CopperLayer):
            label = e.name
        else:
            above, below = names.get(e.above_layer, "top"), names.get(e.below_layer, "bottom")
            label = f"Dielectric {above} – {below}"
            if e.material:
                label += f" ({e.material})"
        rows.append((label, f"{e.thickness_mm:.4f} mm"))
    return rows
