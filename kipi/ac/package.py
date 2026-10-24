"""Package PI models in the AC solve (#53).

A package model is an N-port (:mod:`kipi.core.model.touchstone`) on one board part. Its
PCB ports meet the board at the part's pins: each is a port of the board's field solve,
plus side on the rail's pins and reference side on the ground's. Its die ports have no
place on the board; each is a probe (:attr:`kipi.ac.model.Probe.package`). The rest are
left open or shorted.

Once the parts are on their ports, the board seen at its probes and the package balls is
an impedance matrix Z (:func:`kipi.ac.solver.sweep.terminate_transfer`). The packages
are joined to it by their admittance matrices (Y exists for a package's series paths,
where Z need not): with the ball voltages shared and the ball currents opposite,

    (I + Z_BB Y_BB) V_B + Z_BB Y_BD V_D = Z_BP I_P
    Y_DB V_B + Y_DD V_D = I_D
    Z_PB Y_BB V_B + Z_PB Y_BD V_D + V_P = Z_PP I_P

and a unit current into each probe (board or die) in turn, the others open, gives its
impedance. A package also loads the board probes, which see it through the balls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from kipi.ac.model import DIE, OPEN, PCB, SHORT, PackageModel, PackagePort, Probe

if TYPE_CHECKING:
    from kipi.core.model.board import BoardSnapshot
    from kipi.dc.pi.model import PISetup

# Port names that say a port is on the die side, and ones that say it is on the balls.
_DIE_WORDS = re.compile(r"(?<![a-z])(die|bump|bumps|chip|c4|ubump|pad)(?![a-z])", re.I)
_BALL_WORDS = re.compile(r"(?<![a-z])(bga|ball|balls|pcb|board|pkg_ball)(?![a-z])", re.I)


class PackageError(ValueError):
    """A package model the solve cannot use, with why."""


def die_probe_name(pkg: PackageModel, port: PackagePort) -> str:
    return f"{pkg.name} {port.name or f'port {port.number}'}"


def die_pins(pkg: PackageModel, port: PackagePort):
    """The package part's pins on a die port's nets, as a probe's rail and ground: what puts
    the probe on its rail (and the board map's marker on the part)."""
    from kipi.dc.pi.model import PinRef
    return (PinRef(pkg.ref, port.net, []) if pkg.ref and port.net else None,
            PinRef(pkg.ref, port.ground_net, []) if pkg.ref and port.ground_net else None)


def sync_die_probes(setup: "PISetup") -> bool:
    """Give every package die port a probe, and only those: new ones named after the port,
    each one's rail and ground the part's pins on the port's nets. The probes' own names
    and targets are kept. Whether anything changed."""
    changed = False
    wanted: Dict[Tuple[str, int], Tuple[PackageModel, PackagePort]] = {
        (k.name, p.number): (k, p) for k in setup.packages for p in k.ports if p.role == DIE}
    kept = []
    for pr in setup.probes:
        if not pr.on_die:
            kept.append(pr)
            continue
        hit = wanted.pop((pr.package, pr.die_port), None)
        if hit is None:
            changed = True                  # its package or die port is gone
            continue
        rail, ground = die_pins(*hit)
        if rail is not None and rail != pr.rail or ground is not None and ground != pr.ground:
            pr.rail, pr.ground = rail or pr.rail, ground or pr.ground
            changed = True
        kept.append(pr)
    names = {p.name for p in kept}
    for (pkg_name, number), (k, p) in wanted.items():
        rail, ground = die_pins(k, p)
        if rail is None or ground is None:
            continue                        # no nets yet: no rail to put it on
        name, i = die_probe_name(k, p), 2
        base = name
        while name in names:
            name, i = f"{base} {i}", i + 1
        names.add(name)
        kept.append(Probe(name, rail, ground, package=pkg_name, die_port=number))
        changed = True
    if changed:
        setup.probes[:] = kept
    return changed


def open_removed_dies(setup: "PISetup") -> bool:
    """Leave open the die ports whose probes the user removed. Whether any were."""
    have = {(p.package, p.die_port) for p in setup.probes if p.on_die}
    changed = False
    for k in setup.packages:
        for p in k.ports:
            if (p.role == DIE and k.ref and p.net and p.ground_net
                    and (k.name, p.number) not in have):
                p.role = OPEN
                changed = True
    return changed


def rename_package(setup: "PISetup", old: str, new: str):
    """Keep the die probes pointing at a package that was renamed."""
    for pr in setup.probes:
        if pr.package == old:
            pr.package = new


def guess_ports(pkg: PackageModel, snap: Optional["BoardSnapshot"], names: Sequence[str],
                ground: str = "") -> List[PackagePort]:
    """Ports for a newly read model with ``names``: a port named for one of the part's
    nets is on that net, on the die if its name says so (``_DIE`` / ``bump``) and on the
    PCB otherwise; any other port is left open. ``ground`` is the ground net to use, else
    one of the part's ground nets."""
    nets = part_nets(snap, pkg.ref)
    gnd = ground if ground in nets else next((n for n in nets if _is_ground(n)), "")
    rails = sorted((n for n in nets if n != gnd and not _is_ground(n)), key=len, reverse=True)
    out = []
    for i, name in enumerate(names, 1):
        key = _key(name)
        net = next((n for n in rails if _key(n) and _key(n) in key), "")
        if net and _DIE_WORDS.search(name.replace("_", " ")):
            role = DIE
        elif net:
            role = PCB
        else:
            role = OPEN
        out.append(PackagePort(i, name, role, net, [], gnd if net else "", []))
    return out


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().lstrip("/"))


def _is_ground(net: str) -> bool:
    from kipi.core.model.nets import is_ground_name
    return is_ground_name(net)


def part_nets(snap: Optional["BoardSnapshot"], ref: str) -> List[str]:
    """The nets ``ref`` has pads on, sorted."""
    if snap is None or not ref:
        return []
    return sorted({p.net for p in snap.pads if p.footprint == ref and p.net})


# ---------------------------------------------------------------------- the solve
@dataclass
class PackageUse:
    """How one package joins a rail's solve: its ports on the board (by port number, in the
    order of their board ports) and on the die, the rest open or shorted."""
    model: PackageModel
    balls: List[PackagePort] = field(default_factory=list)
    dies: List[PackagePort] = field(default_factory=list)
    opened: List[PackagePort] = field(default_factory=list)
    shorted: List[PackagePort] = field(default_factory=list)

    def y_at(self, freqs) -> np.ndarray:
        """The package's admittance matrix at ``freqs`` over its ball ports then its die
        ports: the shorted ports dropped (no voltage), the open ones reduced out (no
        current)."""
        net = self.model.network()
        y = net.y_at(freqs)
        keep = [p.number - 1 for p in (*self.balls, *self.dies)]
        out = [p.number - 1 for p in self.opened]
        if not out:
            return y[:, keep][:, :, keep]
        ykk = y[:, keep][:, :, keep]
        yko = y[:, keep][:, :, out]
        yoo = y[:, out][:, :, out]
        yok = y[:, out][:, :, keep]
        return ykk - yko @ np.linalg.pinv(yoo) @ yok


def uses(setup: "PISetup", snap: "BoardSnapshot", rail: Set[str], grounds: Set[str],
         probes: List[Probe]) -> Tuple[List[PackageUse], List[str]]:
    """The packages that join the solve of ``rail`` against ``grounds``, and notes. A
    package joins when it has a PCB port on the rail; its PCB ports on other rails are
    left open, as are its die ports with no probe on this rail. ``probes`` are the die
    probes on the rail."""
    on_die = {(p.package, p.die_port) for p in probes if p.on_die}
    out, notes = [], []
    refs = {f.reference for f in snap.footprints}
    for k in setup.packages:
        use = PackageUse(k)
        for p in sorted(k.ports, key=lambda p: p.number):
            if p.role == PCB and p.net in rail and p.ground_net in grounds:
                use.balls.append(p)
            elif p.role == DIE and (k.name, p.number) in on_die:
                use.dies.append(p)
            elif p.role == SHORT:
                use.shorted.append(p)
            else:
                use.opened.append(p)
        if not use.balls:
            if use.dies:
                notes.append(f"Package {k.name} has no PCB port on this rail and ground; "
                             "its die probes are left out")
            continue
        if k.ref not in refs:
            notes.append(f"Package {k.name}: {k.ref or 'its part'} is not on the board; "
                         "left out")
            continue
        others = [p for p in use.opened if p.role == PCB]
        if others:
            notes.append(f"Package {k.name}: "
                         + ", ".join(p.label for p in others)
                         + " on another rail or ground, left open")
        out.append(use)
    return out, notes


def check_coverage(use: PackageUse, f_lo: float, f_hi: float) -> Optional[str]:
    net = use.model.network()
    if net.covers(f_lo, f_hi):
        return None
    lo, hi = net.freqs_hz[0], net.freqs_hz[-1]
    return (f"Package {use.model.name}: its model covers {_hz(lo)} to {_hz(hi)}; outside that "
            "it is held at its first and last frequency")


def _hz(f: float) -> str:
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if f >= scale * 0.9995:
            return f"{f / scale:.4g} {unit}"
    return f"{f:.4g} Hz"


def connect(zr: np.ndarray, n_probes: int, y: np.ndarray, n_balls: int) -> np.ndarray:
    """The impedance at each board probe and then each die port (F, n_probes + n_dies).

    ``zr`` (F, P + B, P + B) is the board's impedance at its probes then the package balls,
    every part on its port; ``y`` (F, B + D, B + D) the packages' admittance over their
    balls (in the same order) then their die ports, block diagonal across packages."""
    nf = zr.shape[0]
    P, B = n_probes, n_balls
    D = y.shape[1] - B
    out = np.zeros((nf, P + D), complex)
    eye_b = np.eye(B)
    for k in range(nf):
        z, yk = zr[k], y[k]
        zpp, zpb, zbp, zbb = z[:P, :P], z[:P, P:], z[P:, :P], z[P:, P:]
        ybb, ybd, ydb, ydd = yk[:B, :B], yk[:B, B:], yk[B:, :B], yk[B:, B:]
        n = B + D + P
        a = np.zeros((n, n), complex)
        rhs = np.zeros((n, P + D), complex)
        a[:B, :B] = eye_b + zbb @ ybb
        a[:B, B:B + D] = zbb @ ybd
        rhs[:B, :P] = zbp
        a[B:B + D, :B] = ydb
        a[B:B + D, B:B + D] = ydd
        rhs[B:B + D, P:] = np.eye(D)
        a[B + D:, :B] = zpb @ ybb
        a[B + D:, B:B + D] = zpb @ ybd
        a[B + D:, B + D:] = np.eye(P)
        rhs[B + D:, :P] = zpp
        x = np.linalg.solve(a, rhs)
        out[k, :P] = np.diagonal(x[B + D:, :P])
        out[k, P:] = np.diagonal(x[B:B + D, P:])
    return out


def block_diag(ys: List[np.ndarray], n_balls: List[int]) -> np.ndarray:
    """The packages' admittances (each over its balls then dies) as one matrix over every
    package's balls, then every package's dies."""
    nf = ys[0].shape[0]
    B, D = sum(n_balls), sum(y.shape[1] - b for y, b in zip(ys, n_balls))
    out = np.zeros((nf, B + D, B + D), complex)
    bi, di = 0, B
    for y, b in zip(ys, n_balls):
        d = y.shape[1] - b
        idx = np.r_[np.arange(bi, bi + b), np.arange(di, di + d)].astype(int)
        out[:, idx[:, None], idx[None, :]] = y
        bi, di = bi + b, di + d
    return out


def validate(setup: "PISetup") -> List[str]:
    """Problems with the setup's package models, for :func:`kipi.ac.checks.validate_ac`."""
    from kipi.core.model.touchstone import TouchstoneError
    errors = []
    names = [k.name for k in setup.packages]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        errors.append(f"Package model names must be unique: {', '.join(dupes)}")
    for k in setup.packages:
        try:
            n = k.network().n_ports
        except TouchstoneError as e:
            errors.append(f"Package {k.name}: its model cannot be read ({e})")
            continue
        if not k.ref:
            errors.append(f"Package {k.name}: pick the part it is on")
        for p in k.ports:
            if not 1 <= p.number <= n:
                errors.append(f"Package {k.name}: port {p.number} is not in its {n}-port "
                              "model")
            elif p.role in (PCB, DIE) and not (p.net and p.ground_net):
                errors.append(f"Package {k.name}: {p.label} needs its net and ground net")
        if not any(p.role == PCB for p in k.ports):
            errors.append(f"Package {k.name}: connect at least one port to the PCB")
    dies = {(k.name, p.number) for k in setup.packages for p in k.ports if p.role == DIE}
    for pr in setup.probes:
        if pr.on_die and (pr.package, pr.die_port) not in dies:
            errors.append(f"Probe {pr.name}: package {pr.package} has no die port "
                          f"{pr.die_port}")
    return errors
