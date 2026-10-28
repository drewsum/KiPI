"""Build the 3D view's nets in several processes (#210).

Each net's copper and meshes are built apart from every other's, mostly in Python and
shapely, which hold the GIL: on one core a large board takes half a minute. A pool of
worker processes, each given the board once, builds the nets in parallel and returns
their meshes as plain arrays (:class:`~kipi.core.viz.batch.Packed`), which cross between
processes in a fraction of the time a pyvista mesh takes.

Wherever a pool can't start or breaks, the nets left are built here as before.
"""

from __future__ import annotations

from typing import Dict, Iterator, List, Optional, Union

from shapely.geometry.base import BaseGeometry

from kipi.core import workers as pool_workers
from kipi.core.model.board import BoardSnapshot
from kipi.core.progress import NULL, ProgressReporter
from kipi.core.viz.meshes import NetScene
from kipi.core.workers import POOL_ERRORS, BoardPool, board, finished

#: Below this many nets the pool's start (a second or two) costs more than it saves.
MIN_NETS = 40


def workers(n_nets: int) -> int:
    """How many processes to build ``n_nets`` nets with; 0 to build them here."""
    if n_nets < MIN_NETS:
        return 0
    return pool_workers.workers(n_nets // 8)


def _build(net: str, copper: Optional[Dict[int, BaseGeometry]],
           layers: Optional[List[int]] = None, holes: bool = True):
    """One net's copper and packed scene; with ``layers``, only the copper on those, and
    the vias and pads only if ``holes``: one piece of a net built in several."""
    from kipi.core.geometry.copper import net_copper, net_layer_copper
    from kipi.core.viz.batch import pack_scene
    from kipi.core.viz.meshes import build_net_scene
    snap = board()
    if copper is None and layers is None:
        copper = net_copper(snap, net)
    elif copper is None:
        copper = {lid: g for lid, g in ((lid, net_layer_copper(snap, net, lid))
                                        for lid in layers) if not g.is_empty}
    return net, copper, pack_scene(build_net_scene(snap, net, copper, holes=holes))


class Coatings:
    """The solder masks and silkscreens, from :func:`build_nets`: ``(kind, mesh, color)``
    each, as :func:`~kipi.core.viz.meshes.board_coatings` makes them."""

    def __init__(self, coatings):
        self.coatings = coatings


def _coatings(side: str):
    from kipi.core.viz.batch import pack
    from kipi.core.viz.meshes import board_coatings
    return [(kind, pack(mesh), color) for kind, mesh, color in board_coatings(board(), [side])]


def build_nets(snap: BoardSnapshot, nets: List[str], cache: Dict[str, Dict],
               progress: ProgressReporter = NULL, coatings: bool = False
               ) -> Iterator[Union[NetScene, Coatings]]:
    """Each of ``nets``' scenes as it is built, in any order, and with ``coatings`` the
    board's :class:`Coatings`. The nets' copper goes in ``cache`` (net -> layer ->
    geometry), and copper already there is used."""
    left = list(nets)
    wanted = [coatings]                 # still to build
    n = workers(len(left))
    if n:
        try:
            yield from _in_pool(snap, left, cache, n, progress, wanted)
        except POOL_ERRORS as e:
            progress.warn(f"Could not build the view in {n} processes ({e}); building "
                          f"the {len(left)} net(s) left in one")
    from kipi.core.geometry.copper import net_copper
    from kipi.core.viz.meshes import build_net_scene
    for net in left:
        progress.check_cancel()
        if net not in cache:
            cache[net] = net_copper(snap, net, progress)
        yield build_net_scene(snap, net, cache[net], progress)
    if wanted[0]:                       # hidden until asked for, so after the copper
        from kipi.core.viz.meshes import board_coatings
        progress.check_cancel()
        with progress.timed("Building solder masks and silkscreens"):
            yield Coatings(board_coatings(snap))


def _in_pool(snap, left: List[str], cache, n: int, progress, wanted: list):
    """Build ``left`` in ``n`` processes, removing each net from it once yielded, and the
    coatings if ``wanted[0]``, clearing it once they are."""
    from kipi.core.viz import meshes
    with BoardPool(snap, n, dict(meshes._layer_colors)) as pool:
        progress.debug(f"Building {len(left)} net(s) in {n} processes")
        # The biggest nets first, so a ground plane isn't left to finish on its own; a
        # net bigger than a worker's share is built a layer to a task.
        size = {net: sum(map(len, snap.items_on_net(net))) for net in left}
        share = sum(size.values()) / n
        pending, pieces = set(), {}
        # The coatings first, a side to a task: they are the longest tasks.
        coats = [pool.submit(_coatings, side) for side in ("top", "bottom")] if wanted[0] else []
        pending.update(coats)
        coated: Dict[int, list] = {}
        for net in sorted(left, key=size.get, reverse=True):
            if size[net] > share and net not in cache:
                lids = [cu.id for cu in snap.stackup.copper]
                pieces[net] = [len(lids) + 1, {}, None]      # pieces left, copper, scene
                pending |= {pool.submit(_build, net, None, [lid], False) for lid in lids}
                pending.add(pool.submit(_build, net, None, [], True))   # vias and pads
            else:
                pending.add(pool.submit(_build, net, cache.get(net)))
        for f in finished(pending, progress):
            if f in coats:
                coated[coats.index(f)] = f.result()
                if len(coated) == len(coats):
                    from kipi.core.viz.batch import unpack
                    wanted[0] = False
                    yield Coatings([(kind, unpack(m), color) for k in sorted(coated)
                                    for kind, m, color in coated[k]])
                continue
            net, copper, ns = f.result()
            if net in pieces:
                ns = _join(snap, pieces[net], copper, ns)
                if ns is None:
                    continue
                copper = pieces.pop(net)[1]
            cache[net] = copper
            left.remove(net)
            yield ns


def _join(snap, piece: list, copper, ns) -> Optional[NetScene]:
    """Add one piece of a net to what has come of it (``[pieces left, copper, scene]``);
    once all have, the whole net's scene, its copper in stackup order."""
    piece[0] -= 1
    piece[1].update(copper)
    if piece[2] is None:
        piece[2] = ns
    else:
        piece[2].copper += ns.copper
        if ns.vias is not None or ns.pads:
            piece[2].vias, piece[2].pads = ns.vias, ns.pads
    if piece[0]:
        return None
    order = {cu.id: k for k, cu in enumerate(snap.stackup.copper)}
    piece[1] = dict(sorted(piece[1].items(), key=lambda kv: order[kv[0]]))
    piece[2].copper.sort(key=lambda lm: order[lm.layer_id])
    return piece[2]
