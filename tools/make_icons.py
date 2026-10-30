"""KiPI icons from resources/kipi_1080.png: the 24/48/256 px PNGs, the .ico, and the
animated logo (the planes flashing through result maps, as the 0.7.0 gif did).

Run: python tools/make_icons.py [out_dir]  (default: resources). Needs Pillow, NumPy and
matplotlib (all in KiPI's environment)."""

import sys
from pathlib import Path

import numpy as np
from matplotlib import colormaps
from PIL import Image

RESOURCES = Path(__file__).resolve().parents[1] / "resources"
SRC = RESOURCES / "kipi_1080.png"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else RESOURCES
PNG_SIZES = (24, 48, 256)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
GIF_SIZE = 256
CYCLE, FRAME_MS = 31, 50                  # frames per result map, ms per frame
SWEEP_START, SWEEP_STAGGER, SWEEP_LEN = 1, 2, 8
FADE_START, FADE_LEN = 18, 12
# Plane fills, top to bottom.
FILLS = [(237, 28, 36), (252, 238, 33), (0, 113, 188)]


def plane_weights(rgb, alpha):
    """Per plane, each pixel's share of its fill (the rest is the black outline)."""
    out = []
    for fill in FILLS:
        f = np.array(fill, float)
        a = np.clip((rgb @ f) / (f @ f), 0, 1)
        resid = np.linalg.norm(rgb - a[..., None] * f, axis=-1)
        w = np.where((resid < 14) & (alpha > 0), a, 0.0)
        out.append(w)
    return out


def plane_coords(w):
    """(u, v): 0..1 left to right over the plane, -1..1 top to bottom about its middle."""
    ys, xs = np.nonzero(w > 0.99)
    x0, x1 = xs.min(), xs.max()
    yc = ys.mean()
    half = (ys.max() - ys.min()) / 2
    yy, xx = np.mgrid[0:w.shape[0], 0:w.shape[1]].astype(float)
    return (xx - x0) / (x1 - x0), (yy - yc) / half


def g(u, v, cu, cv, su, sv=None):
    sv = sv or su * 2.2
    return np.exp(-(((u - cu) / su) ** 2 + ((v - cv) / sv) ** 2))


def voltage(u, v, k):
    """A drop map: high at the source end, a dip at a load (turbo)."""
    src = [0.06, 0.18, 0.10][k]
    load = [0.80, 0.62, 0.74][k]
    f = 0.55 + 0.4 * g(u, v, src, 0.0, 0.16) - 0.55 * g(u, v, load, 0.1, 0.12)
    f += 0.18 * (0.5 - u) + 0.05 * np.sin(5 * u + 2 * v + k)
    return np.clip(f, 0, 1)


def thermal(u, v, k):
    """Hot spots on a cool board (inferno)."""
    spots = [[(0.12, 0.0), (0.66, 0.2)], [(0.32, -0.1), (0.78, 0.15)], [(0.18, 0.1), (0.55, -0.1)]]
    f = 0.12 + 0.1 * (1 - u)
    for cu, cv in spots[k]:
        f = f + 0.85 * g(u, v, cu, cv, 0.11)
    return np.clip(f, 0, 1)


def current(u, v, k):
    """Current crowding into a via: a bright core with rings around it (jet)."""
    cu = [0.12, 0.42, 0.68][k]
    r = np.hypot((u - cu) / 1.0, v / 2.2)
    f = 0.95 * np.exp(-(r / 0.07) ** 2) + 0.55 * np.exp(-((r - 0.16) / 0.05) ** 2)
    f += 0.2 + 0.2 * (1 - u) + 0.15 * np.exp(-(r / 0.45) ** 2)
    return np.clip(f, 0, 1)


MAPS = [(voltage, "turbo"), (thermal, "inferno"), (current, "jet")]


def smooth(t):
    t = np.clip(t, 0, 1)
    return t * t * (3 - 2 * t)


def frames(src):
    a = np.asarray(src.convert("RGBA")).astype(float)
    rgb, alpha = a[..., :3], a[..., 3]
    ws = plane_weights(rgb, alpha)
    coords = [plane_coords(w) for w in ws]
    out = []
    for field, cmap_name in MAPS:
        cmap = colormaps[cmap_name]
        maps = [cmap(field(u, v, k))[..., :3] * 255 for k, (u, v) in enumerate(coords)]
        for f in range(CYCLE):
            img = rgb.copy()
            fade = smooth((f - FADE_START) / FADE_LEN)
            for k, (w, (u, v), m) in enumerate(zip(ws, coords, maps)):
                fill = np.array(FILLS[k], float)
                t = (f - SWEEP_START - SWEEP_STAGGER * k) / SWEEP_LEN
                front = -0.2 + 1.45 * np.clip(t, 0, 1.2)
                us = u + 0.12 * v                          # a slanted wipe
                shown = np.clip((front - us) / 0.04 + 0.5, 0, 1) if t > 0 else 0 * us
                band = 0.85 * np.exp(-((us - front) / 0.05) ** 2) if 0 < t < 1.1 else 0
                col = fill + shown[..., None] * (m - fill)
                col = col + (np.asarray(band)[..., None] if np.ndim(band) else band) * (255 - col)
                col = col + fade * (fill - col)
                img = img + w[..., None] * (col - fill)    # the outline's share stays black
            out.append(Image.fromarray(np.dstack([np.clip(img, 0, 255),
                                                  alpha]).astype(np.uint8), "RGBA"))
    return out


def to_gif_frame(im):
    """A 255-colour palette frame with index 255 transparent."""
    a = np.asarray(im)
    rgb = Image.fromarray(a[..., :3], "RGB")
    q = rgb.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    idx = np.asarray(q).copy()
    idx[a[..., 3] < 128] = 255
    pal = q.getpalette()[:255 * 3]
    pal += [0, 0, 0] * (256 - len(pal) // 3)
    p = Image.fromarray(idx, "P")
    p.putpalette(pal)
    p.info["transparency"] = 255
    return p


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    src = Image.open(SRC).convert("RGBA")
    for s in PNG_SIZES:
        src.resize((s, s), Image.Resampling.LANCZOS).save(OUT / f"kipi_{s}.png", optimize=True)
    icos = [src.resize((s, s), Image.Resampling.LANCZOS) for s in ICO_SIZES]
    icos[-1].save(OUT / "kipi.ico", sizes=[(s, s) for s in ICO_SIZES], append_images=icos[:-1])
    big = frames(src)
    small = [to_gif_frame(f.resize((GIF_SIZE, GIF_SIZE), Image.Resampling.LANCZOS))
             for f in big]
    small[0].save(OUT / "kipi.gif", save_all=True, append_images=small[1:], duration=FRAME_MS,
                  loop=0, disposal=2, transparency=255, optimize=False)
    print(f"{len(small)} frames")


if __name__ == "__main__":
    main()
