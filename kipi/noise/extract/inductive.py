"""Inductive (magnetic-field) coupling from a switcher's current loops onto a victim (#69).

The sources are each capacitor's current loop (:mod:`kipi.noise.parasitics`): in a buck,
the input capacitors' hot loops through the switching device, which carry the pulsed
input current, and the output capacitors' loops through the inductor, which carry its
ripple; in a boost, the other way round. Each carries its capacitor's ripple current, as
the waveforms share it out.

A victim segment links the loops' flux through the loop it makes with its return current,
which flows in the plane under it. The plane under the loops answers their field with eddy
currents, an image of the loop of strength Γ: a thin copper sheet is transparent to a
loop ℓ across at low frequency and a mirror at high, Γ = jα/(1 + jα) with
α = ω·μ0·(t/ρ)·ℓ/2 (about 0.3 at 10 MHz for a 4 mm loop over 35 µm of copper). On the
loops' side of the plane the victim's EMF is

    E = −jω·Σ I·(M(loop, segment) − Γ·M(loop image, segment)),

the partial mutual inductances from :mod:`kipi.noise.extract.peec`. A victim on the far
side links what the plane lets through, (1 − Γ)·1/cosh((1+j)·t/δ) of the field (the skin
effect stops it in thick copper), between itself and the plane's far face, where its
return flows. Where no plane covers the victim, the free-space mutual inductance is used.

**Vias** (#173). The loops' via drops are vertical segments, and so are the victim's own
vias: where its traces change layer, the via between the two layers carries its current
and links the loops' field like any trace, which is how a via beside a hot loop's via
picks up noise. Each via is cut at the copper layers it passes, so each piece lies wholly
on one side of any plane, and its EMF is added in series to a victim segment that ends at
it on its first layer (signed by the way the path crosses it). A via's stub beyond the
layers its traces use carries no current and is left out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from kipi.core.model.board import BoardSnapshot
from kipi.noise.extract import peec
from kipi.noise.extract.capacitive import Segment as VictimSegment

COPPER_T_MM = 0.035
# Where a via's plane cover is looked for: beside its antipad, at 1 mm each way.
VIA_COVER_MM = ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0))


@dataclass
class LoopSource:
    label: str
    segments: List[peec.Segment]
    current: np.ndarray                 # Fourier coefficients along the loop's direction
    image_z: Optional[float] = None     # the plane under the loop, if any
    image_layer: Optional[int] = None


def loop_sources(layouts, wf) -> List[LoopSource]:
    """Every capacitor loop of every phase, with its current. A capacitor's spectrum is
    the current into its positive pad; the loop runs out of that pad, so it carries the
    negative of it."""
    out = []
    for i, lay in enumerate(layouts, 1):
        for ref, segs in lay.loops.items():
            spec = wf.input_caps.get(ref) or wf.output_caps.get(ref)
            if spec is None:
                continue
            out.append(LoopSource(f"{ref} loop (phase {i})", segs, -spec.c, lay.image_z,
                                  lay.image_layer))
    return out


def _victim_3d(snap: BoardSnapshot, segs: Sequence[VictimSegment]) -> List[peec.Segment]:
    out = []
    for s in segs:
        lay = snap.stackup.layer(s.layer)
        z = lay.z_mid_mm if lay else 0.0
        t = lay.thickness_mm if lay else COPPER_T_MM
        out.append(peec.segment((*s.a, z), (*s.b, z), s.width_mm, t))
    return out


@dataclass
class ViaPiece:
    """A piece of a victim via between two copper layers, as the segment it is in series
    with: its EMF along it (down the path) adds sign × to segment ``seg``'s, a→b."""
    piece: peec.Segment
    seg: int
    sign: float


def via_pieces(snap: BoardSnapshot, net: str, segs: Sequence[VictimSegment]
               ) -> List[ViaPiece]:
    """The vertical pieces of ``net``'s vias that join its segments on different layers."""
    _, vias, _, _ = snap.items_on_net(net)
    z = {c.id: c.z_mid_mm for c in snap.stackup.copper}
    out: List[ViaPiece] = []
    for via in vias:
        r = max(via.diameter_mm / 2, 1e-3)
        ends: Dict[int, Tuple[int, float]] = {}       # layer -> (segment, sign)
        for i, s in enumerate(segs):
            if s.layer in ends or s.layer not in z:
                continue
            if np.hypot(s.b[0] - via.position[0], s.b[1] - via.position[1]) <= r:
                ends[s.layer] = (i, 1.0)              # the path runs a→b into the via
            elif np.hypot(s.a[0] - via.position[0], s.a[1] - via.position[1]) <= r:
                ends[s.layer] = (i, -1.0)
        if len(ends) < 2:
            continue
        used = sorted(ends, key=lambda lid: -z[lid])
        # The tube's geometric mean radius is its radius: w = t with GMD 0.2235·(w + t).
        w = via.diameter_mm / (4 * peec.GMD_FACTOR)
        x, y = via.position
        cuts = sorted({z[lid] for lid in z if z[used[-1]] <= z[lid] <= z[used[0]]},
                      reverse=True)
        for top, bottom in zip(used, used[1:]):
            seg, sign = ends[top]
            levels = [h for h in cuts if z[bottom] <= h <= z[top]]
            for z0, z1 in zip(levels, levels[1:]):
                out.append(ViaPiece(peec.segment((x, y, z0), (x, y, z1), w, w,
                                                 f"via {via.id}"), seg, sign))
    return out


def _plane_cover(snap: BoardSnapshot, layer: int, xy: np.ndarray) -> np.ndarray:
    """Whether a plane (any zone fill) on ``layer`` covers each point."""
    import shapely
    from kipi.core.geometry.copper import poly_to_shapely
    polys = [poly_to_shapely(p) for z in snap.zones for p in z.filled.get(layer, [])]
    polys = [p for p in polys if p is not None and not p.is_empty]
    if not polys:
        return np.zeros(len(xy), dtype=bool)
    return shapely.contains_xy(shapely.union_all(polys), xy[:, 0], xy[:, 1])


def _loop_size(src: LoopSource) -> float:
    """A loop's size across, mm: the square root of its outline's area seen from above,
    at least 1 mm."""
    pts = np.array([s.a[:2] for s in src.segments])
    if len(pts) < 3:
        return 1.0
    x, y = pts[:, 0], pts[:, 1]
    area = 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
    return max(float(np.sqrt(area)), 1.0)


def victim_emf(snap: BoardSnapshot, sources: Sequence[LoopSource],
               segs: Sequence[VictimSegment], freqs: np.ndarray, net: str = ""
               ) -> Tuple[np.ndarray, Dict[str, float]]:
    """The EMF along each victim segment (a→b) at each frequency, (F, segments), and
    each source loop's mutual inductance with the whole victim at the first frequency.
    With ``net``, the victim's vias are included, their EMF in its segments'."""
    emf = np.zeros((len(freqs), len(segs)), complex)
    mutual: Dict[str, float] = {}
    if not sources or not segs:
        return emf, mutual
    pieces = via_pieces(snap, net, segs) if net else []
    victim = _victim_3d(snap, segs) + [p.piece for p in pieces]
    # Each piece's own EMF, folded into its segment's below.
    fold = np.zeros((len(victim), len(segs)))
    fold[np.arange(len(segs)), np.arange(len(segs))] = 1.0
    for k, p in enumerate(pieces):
        fold[len(segs) + k, p.seg] = p.sign
    vz = np.array([(v.a[2] + v.b[2]) / 2 for v in victim])
    mid = np.array([[(v.a[0] + v.b[0]) / 2, (v.a[1] + v.b[1]) / 2] for v in victim])
    omega = 2 * np.pi * np.asarray(freqs)
    for src in sources:
        direct = peec.mutual_matrix(src.segments, victim).sum(axis=0)        # (V,)
        m = np.broadcast_to(direct, (len(freqs), len(victim))).astype(complex)
        lay = snap.stackup.layer(src.image_layer) if src.image_layer is not None else None
        if src.image_z is not None and lay is not None:
            gamma, trans = peec.plane_response(freqs, lay.thickness_mm, _loop_size(src))
            image = peec.mutual_matrix([peec.mirror(s, src.image_z) for s in src.segments],
                                       victim).sum(axis=0)
            src_z = float(np.mean([(s.a[2] + s.b[2]) / 2 for s in src.segments]))
            same = np.sign(vz - src.image_z) == np.sign(src_z - src.image_z)
            covered = _plane_cover(snap, src.image_layer, mid)
            if pieces:
                # A via passes the plane through its antipad: the plane around it counts.
                around = np.array([mid[len(segs):] + d for d in VIA_COVER_MM])
                near = np.array([_plane_cover(snap, src.image_layer, xy) for xy in around])
                covered[len(segs):] = near.any(axis=0)
            inside = (vz >= lay.z_bottom_mm - 1e-9) & (vz <= lay.z_top_mm + 1e-9)
            same &= ~inside
            m = m.copy()
            m[:, same] = direct[same][None, :] - gamma[:, None] * image[same][None, :]
            behind = ~same & covered
            if behind.any():
                # The victim's return flows on the plane's far face: it links the field
                # the plane lets through between itself and that face.
                face = lay.z_bottom_mm if src_z > src.image_z else lay.z_top_mm
                idx = np.flatnonzero(behind)
                proj = [peec.Segment(np.array([*victim[i].a[:2], face]),
                                     np.array([*victim[i].b[:2], face]),
                                     victim[i].w, victim[i].t) for i in idx]
                linked = direct[idx] - peec.mutual_matrix(src.segments, proj).sum(axis=0)
                m[:, idx] = trans[:, None] * linked[None, :]
        m = m @ fold
        emf += -1j * omega[:, None] * src.current[:, None] * m
        mutual[src.label] = float(np.real(m[0].sum()))
    return emf, mutual
