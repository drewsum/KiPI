"""Material physics for the AC model, in KiPI's units (mm, ohms, henries, farads)."""

from __future__ import annotations

import math

import numpy as np

MU0_H_PER_MM = 4e-10 * math.pi            # 4π·10⁻⁷ H/m
EPS0_F_PER_MM = 8.8541878128e-15           # 8.854·10⁻¹² F/m


def skin_depth_mm(rho_ohm_mm: float, omega):
    return np.sqrt(2.0 * rho_ohm_mm / (np.asarray(omega) * MU0_H_PER_MM))


def slab_impedance(rho_ohm_mm: float, t_mm: float, omega: float):
    """Internal impedance of a copper sheet driven from both faces, ohms per square.

    With K1, K2 the current (per unit width) carried by the top and bottom face regions,
    the tangential field at the faces is E1 = Z11 K1 + Z12 K2 and E2 = Z12 K1 + Z11 K2,
    where Z11 = ρk·coth(kt), Z12 = ρk·csch(kt) and k = (1+j)/δ (1D diffusion across the
    thickness). At low frequency both tend to ρ/t: the faces are one conductor with the
    DC sheet resistance. Well above the frequency where δ = t, Z12 vanishes and each face
    is an independent sheet with the surface impedance (1+j)ρ/δ.
    """
    delta = math.sqrt(2.0 * rho_ohm_mm / (omega * MU0_H_PER_MM))
    k = (1 + 1j) / delta
    x = k * t_mm
    if x.real > 30:                       # csch underflows to 0 and coth to 1 cleanly
        return rho_ohm_mm * k, 0j
    if abs(x) < 1e-3:                     # series: keeps the small differences exact
        z11 = rho_ohm_mm / t_mm + 1j * omega * MU0_H_PER_MM * t_mm / 3
        z12 = rho_ohm_mm / t_mm - 1j * omega * MU0_H_PER_MM * t_mm / 6
        return z11, z12
    return rho_ohm_mm * k / np.tanh(x), rho_ohm_mm * k / np.sinh(x)


def slab_difference(rho_ohm_mm: float, t_mm: float, omega: float) -> complex:
    """Z11 − Z12 = ρk·tanh(kt/2), without cancellation (at low frequency it is the small
    jωμ₀t/2 between two values near ρ/t)."""
    delta = math.sqrt(2.0 * rho_ohm_mm / (omega * MU0_H_PER_MM))
    k = (1 + 1j) / delta
    x = k * t_mm
    if x.real > 30:
        return rho_ohm_mm * k
    return rho_ohm_mm * k * np.tanh(x / 2)



# Copper wider than this many gap heights keeps the cavity model's inductance whole: the
# correction below reaches 1 there (#258).
FRINGE_NONE = 2.3


def fringe_factor(w_mm, gap_mm):
    """What the inductance height of a face of copper ``w_mm`` wide is scaled by, where it
    faces wider copper of the other side across ``gap_mm`` (#258).

    The cavity model puts μ₀·gap/2 per square on each of the two facing faces. Under copper
    much narrower than the plane it faces, a BGA dogbone or a capacitor's fanout trace, the
    plane's share hardly counts: its current spreads out sideways, where the faces see
    nothing narrower. The strip's loop inductance is then its own face's, μ₀·gap/(2w) per
    length, which ignores the field fringing past its edges: 1.2 nH/mm for 0.25 mm at
    0.48 mm, where the microstrip's (Hammerstad, in air: L = Z₀/c) is 0.55. The factor
    2·L·w/(μ₀·gap) brings the face to the microstrip's; it reaches 1 at about FRINGE_NONE
    gap heights, so wider copper and planes are as before.
    """
    w = np.maximum(np.asarray(w_mm, dtype=float), 1e-6)
    # np.inf (a plane): no correction
    u = np.minimum(w / np.maximum(np.asarray(gap_mm, dtype=float), 1e-9), 2 * FRINGE_NONE)
    z0 = np.where(u <= 1,
                  60.0 * np.log(8.0 / u + u / 4.0),
                  120.0 * np.pi / (u + 1.393 + 0.667 * np.log(u + 1.444)))
    # L per length over μ₀·gap/w is Z₀/c · w / (μ₀ gap) = Z₀ u / η₀.
    return np.minimum(2.0 * z0 * u / (120.0 * np.pi), 1.0)
