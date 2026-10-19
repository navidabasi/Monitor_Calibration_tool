"""Color math: sRGB/XYZ, CCT and Duv, CIELAB and Delta E 2000."""

import numpy as np

SRGB_TO_XYZ = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
])
XYZ_TO_SRGB = np.linalg.inv(SRGB_TO_XYZ)
D65 = SRGB_TO_XYZ.sum(axis=1)  # white with Y = 1
SRGB_PRIMARIES_XY = [(0.64, 0.33), (0.30, 0.60), (0.15, 0.06)]


def xy(xyz):
    s = float(np.sum(xyz))
    return (float(xyz[0] / s), float(xyz[1] / s)) if s > 0 else (0.0, 0.0)


def uv1960(xyz):
    x, y, z = xyz
    d = x + 15 * y + 3 * z
    return 4 * x / d, 6 * y / d


def planck_uv(t):
    """Planckian locus in CIE 1960 uv (Krystek's approximation, 1000-15000 K)."""
    t = np.asarray(t, dtype=float)
    u = (0.860117757 + 1.54118254e-4 * t + 1.28641212e-7 * t ** 2) / (1 + 8.42420235e-4 * t + 7.08145163e-7 * t ** 2)
    v = (0.317398726 + 4.22806245e-5 * t + 4.20481691e-8 * t ** 2) / (1 - 2.89741816e-5 * t + 1.61456053e-7 * t ** 2)
    return u, v


def cct_duv(xyz):
    """Correlated color temperature (K) and Duv. Positive Duv = greenish, negative = magenta."""
    u, v = uv1960(xyz)
    ts = np.geomspace(1500, 20000, 3000)
    pu, pv = planck_uv(ts)
    i = int(np.argmin((pu - u) ** 2 + (pv - v) ** 2))
    lo, hi = ts[max(i - 1, 0)], ts[min(i + 1, len(ts) - 1)]
    g = (np.sqrt(5) - 1) / 2
    for _ in range(60):  # golden-section refine
        a, b = hi - g * (hi - lo), lo + g * (hi - lo)
        da = np.hypot(*(np.array(planck_uv(a)) - (u, v)))
        db = np.hypot(*(np.array(planck_uv(b)) - (u, v)))
        if da < db:
            hi = b
        else:
            lo = a
    t = (lo + hi) / 2
    pu, pv = planck_uv(t)
    tu, tv = np.array(planck_uv(t + 1)) - np.array(planck_uv(t - 1))
    cross = tu * (v - pv) - tv * (u - pu)
    duv = float(np.hypot(u - pu, v - pv) * -np.sign(cross))
    return float(t), duv


def xyz_to_lab(xyz, white):
    t = np.asarray(xyz, dtype=float) / np.asarray(white, dtype=float)
    d = 6 / 29
    f = np.where(t > d ** 3, np.cbrt(t), t / (3 * d * d) + 4 / 29)
    return np.array([116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])])


def delta_e2000(lab1, lab2):
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    c1, c2 = np.hypot(a1, b1), np.hypot(a2, b2)
    cm = (c1 + c2) / 2
    g = 0.5 * (1 - np.sqrt(cm ** 7 / (cm ** 7 + 25 ** 7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dl = l2 - l1
    dc = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180:
            dhp -= 360
        elif dhp < -180:
            dhp += 360
    dh = 2 * np.sqrt(c1p * c2p) * np.sin(np.radians(dhp / 2))
    lm = (l1 + l2) / 2
    cmp_ = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hm = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hm = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hm = (h1p + h2p + 360) / 2
    else:
        hm = (h1p + h2p - 360) / 2
    t = (1 - 0.17 * np.cos(np.radians(hm - 30)) + 0.24 * np.cos(np.radians(2 * hm))
         + 0.32 * np.cos(np.radians(3 * hm + 6)) - 0.20 * np.cos(np.radians(4 * hm - 63)))
    sl = 1 + 0.015 * (lm - 50) ** 2 / np.sqrt(20 + (lm - 50) ** 2)
    sc = 1 + 0.045 * cmp_
    sh = 1 + 0.015 * cmp_ * t
    rt = (-2 * np.sqrt(cmp_ ** 7 / (cmp_ ** 7 + 25 ** 7))
          * np.sin(np.radians(60 * np.exp(-(((hm - 275) / 25) ** 2)))))
    return float(np.sqrt((dl / sl) ** 2 + (dc / sc) ** 2 + (dh / sh) ** 2 + rt * (dc / sc) * (dh / sh)))


def rgb_balance(xyz):
    """Linear sRGB of a gray divided by its luminance, in percent. D65 gray = (100, 100, 100)."""
    xyz = np.asarray(xyz, dtype=float)
    if xyz[1] <= 0:
        return [0.0, 0.0, 0.0]
    return (XYZ_TO_SRGB @ (xyz / xyz[1]) * 100).tolist()


def target_curve(name):
    if name == "srgb":
        return lambda v: v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    gamma = float(name)
    return lambda v: v ** gamma


def target_gamma(name):
    """The power-law gamma that best describes the target, for comparisons."""
    return 2.2 if name == "srgb" else float(name)


def xy_to_xyz(xy, y_lum=1.0):
    x, y = xy
    return np.array([x / y * y_lum, y_lum, (1 - x - y) / y * y_lum])


def rgb_to_xyz_matrix(primaries, white):
    """Linear RGB -> XYZ for given primaries and white (xy), with white at Y = 1."""
    p = np.column_stack([xy_to_xyz(c) for c in primaries])
    s = np.linalg.solve(p, xy_to_xyz(white))
    return p * s


def triangle_area(pts):
    (x1, y1), (x2, y2), (x3, y3) = pts
    return abs((x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)) / 2
