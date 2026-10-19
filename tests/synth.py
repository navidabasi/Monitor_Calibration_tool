"""Synthetic phone photos of the charts, written as real DNG files.

Simulates a display (primaries, white point, tone curve, black level), a
camera (linear XYZ -> camera RGB), lens falloff with color shading, defocus
blur, veiling glare, sensor noise and a Bayer mosaic, so the full pipeline
(rawpy decode -> detection -> analysis) can be tested without a phone.
"""

import struct

import numpy as np
from scipy import ndimage

from monitor_calibration import chart
from monitor_calibration import colorimetry as cm

P3_TO_XYZ = np.array([
    [0.4865709, 0.2656677, 0.1982173],
    [0.2289746, 0.6917385, 0.0792869],
    [0.0000000, 0.0451134, 1.0439444],
])

# XYZ -> camera RGB. Rows scaled like a real sensor (green most sensitive).
CAMERA = np.array([
    [1.00, -0.10, -0.05],
    [-0.30, 1.20, 0.05],
    [0.05, -0.05, 0.90],
]) * np.array([[0.55], [1.0], [0.75]])

BLACK, WHITE = 64, 1023


class Display:
    def __init__(self, to_xyz, gains=(1, 1, 1), gamma=2.2, luminance=1.0, black=0.0, crush=0):
        self.to_xyz = np.asarray(to_xyz, float)
        self.gains = np.asarray(gains, float)
        self.gamma = gamma
        self.black = black
        self.crush = crush
        self.scale = luminance / (self.to_xyz @ self.gains)[1]

    def xyz(self, rgb_code):
        code = np.asarray(rgb_code, float)
        lin = np.where(code <= self.crush, 0.0, (code / 255.0) ** self.gamma) * self.gains
        white = self.to_xyz @ self.gains * self.scale
        return self.to_xyz @ lin * self.scale + self.black * white

    @property
    def white(self):
        return self.xyz([255, 255, 255])


def _homography(src, dst):
    from monitor_calibration.detect import homography
    return homography(src, dst)


def render(display, chart_type, role, exposure=1.0, size=(1500, 1125), frac=1.0, angle=1.5,
           offset=(6, -4), keystone=0.01, rot180=False, seed=0, glare=0.003):
    """Return a Bayer mosaic (uint16) of the chart as photographed."""
    rng = np.random.default_rng(seed)
    w, h = size
    cw = w / 3 * frac
    chh = cw * 0.75
    cx, cy = w / 2 + offset[0], h / 2 + offset[1]
    a = np.radians(angle)
    corners = np.array([[-cw / 2, -chh / 2], [cw / 2, -chh / 2], [cw / 2, chh / 2], [-cw / 2, chh / 2]])
    corners[:2, 0] *= 1 - keystone  # top edge slightly shorter
    rot = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
    img_pts = corners @ rot.T + (cx, cy)
    hmat = _homography([(0, 0), (100, 0), (100, 75), (0, 75)], img_pts)
    hinv = np.linalg.inv(hmat)

    yy, xx = np.mgrid[0:h, 0:w]
    p = np.c_[xx.ravel(), yy.ravel(), np.ones(xx.size)] @ hinv.T
    u = (p[:, 0] / p[:, 2]).reshape(h, w)
    v = (p[:, 1] / p[:, 2]).reshape(h, w)

    # Element map: -2 outside the screen, -1 black screen, else index into table.
    elem = np.full((h, w), -2, int)
    on_screen = (u > -75) & (u < 175) & (v > -32.8) & (v < 107.8)
    elem[on_screen] = -1
    table = []

    def paint(rect, code):
        x, y, rw, rh = rect
        m = (u >= x) & (u < x + rw) & (v >= y) & (v < y + rh)
        elem[m] = len(table)
        table.append(display.xyz(code))

    mark = chart.MARK_LEVEL[chart_type]
    for fx, fy in chart.FIDUCIALS:
        paint((fx, fy, chart.FIDUCIAL_SIZE, chart.FIDUCIAL_SIZE), (mark,) * 3)
    for i, bit in enumerate(chart.encode_bits(chart_type, role)):
        if bit:
            paint(chart.bit_rect(i), (mark,) * 3)
    for i, (_, rgb) in enumerate(chart.PATCHES[chart_type]):
        paint(chart.patch_rect(i), rgb)

    black_xyz = display.xyz((0, 0, 0))
    cam_table = np.array([CAMERA @ t for t in table])
    scene = np.zeros((h, w, 3))
    scene[elem == -1] = CAMERA @ black_xyz
    for i, c in enumerate(cam_table):
        scene[elem == i] = c
    scene *= exposure

    # Optics: defocus, glare (local halo + veiling), falloff with color shading.
    scene = np.stack([ndimage.gaussian_filter(scene[..., c], 1.6) for c in range(3)], axis=-1)
    halo = np.stack([ndimage.gaussian_filter(scene[..., c], 0.03 * w) for c in range(3)], axis=-1)
    scene = scene + glare * halo + 0.2 * glare * scene.mean(axis=(0, 1))
    r2 = ((xx - w / 2) ** 2 + (yy - h / 2) ** 2) / ((w / 2) ** 2 + (h / 2) ** 2)
    falloff = np.stack([1 - k * r2 for k in (0.42, 0.33, 0.37)], axis=-1)
    scene *= falloff

    if rot180:
        scene = scene[::-1, ::-1]

    # Bayer RGGB at twice the resolution, with noise, in DN.
    dn = scene * (WHITE - BLACK)
    mosaic = np.empty((h * 2, w * 2))
    mosaic[0::2, 0::2] = dn[..., 0]
    mosaic[0::2, 1::2] = dn[..., 1]
    mosaic[1::2, 0::2] = dn[..., 1]
    mosaic[1::2, 1::2] = dn[..., 2]
    noise = rng.normal(size=mosaic.shape) * np.sqrt(np.clip(mosaic, 0, None) * 0.5 + 4.0)
    out = np.clip(np.round(mosaic + noise + BLACK), 0, WHITE)
    return out.astype(np.uint16)


def write_dng(path, mosaic, shutter, iso=50, when="2026:10:18 21:00:00", color_matrix=CAMERA,
              make="Synthetic", model="Test camera", unique="Synthetic test camera", software=None,
              lens=None, extra_ifd0=(), extra_exif=()):
    """Minimal uncompressed Bayer DNG (little-endian TIFF).

    extra_ifd0 / extra_exif add (tag, type, values) entries, for tests that
    check what is read from a file (serial numbers, GPS, owner name, ...).
    """
    h, w = mosaic.shape
    data = mosaic.astype("<u2").tobytes()
    cm_norm = np.asarray(color_matrix, float)

    def rat(x, signed=False):
        den = 1_000_000
        return (int(round(x * den)), den)

    exif = [
        (33434, 5, [rat(shutter)]),             # ExposureTime
        (34855, 3, [iso]),                       # ISOSpeedRatings
        (36867, 2, when),                        # DateTimeOriginal
    ]
    if lens:
        exif += [(42036, 2, lens), (37386, 5, [rat(6.765)]), (33437, 5, [rat(1.78)])]
    exif += list(extra_exif)
    ifd0 = [
        (254, 4, [0]), (256, 4, [w]), (257, 4, [h]), (258, 3, [16]), (259, 3, [1]),
        (262, 3, [32803]), (271, 2, make), (272, 2, model), (273, 4, [0]),
        (274, 3, [1]), (277, 3, [1]), (278, 4, [h]), (279, 4, [len(data)]), (284, 3, [1]),
        (33421, 3, [2, 2]), (33422, 1, [0, 1, 1, 2]), (34665, 4, [0]),
        (50706, 1, [1, 4, 0, 0]), (50708, 2, unique),
        (50710, 1, [0, 1, 2]), (50711, 3, [1]),
        (50714, 4, [BLACK]), (50717, 4, [WHITE]),
        (50721, 10, [rat(x, True) for x in cm_norm.ravel()]),
        (50728, 5, [rat(1.0), rat(1.0), rat(1.0)]),
        (50778, 3, [21]),
    ]
    if software:
        ifd0.append((305, 2, software))
    ifd0 += list(extra_ifd0)
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 10: 8}

    def encode(typ, vals):
        if typ == 2:
            b = vals.encode("ascii") + b"\0"
            return b, len(b)
        if typ in (1, 7):
            return bytes(vals), len(vals)
        if typ == 3:
            return struct.pack(f"<{len(vals)}H", *vals), len(vals)
        if typ == 4:
            return struct.pack(f"<{len(vals)}I", *vals), len(vals)
        if typ == 5:
            return b"".join(struct.pack("<II", *v) for v in vals), len(vals)
        if typ == 10:
            return b"".join(struct.pack("<ii", *v) for v in vals), len(vals)
        raise ValueError(typ)

    def ifd_size(entries):
        extra = sum((lambda b: len(b) + (len(b) & 1) if len(b) > 4 else 0)(encode(t, v)[0]) for _, t, v in entries)
        return 2 + 12 * len(entries) + 4 + extra

    off_ifd0 = 8
    off_exif = off_ifd0 + ifd_size(ifd0)
    off_data = off_exif + ifd_size(exif)
    off_data += off_data & 1
    ifd0 = [(t, ty, [off_data] if t == 273 else [off_exif] if t == 34665 else v) for t, ty, v in ifd0]

    def build(entries, start, next_ifd=0):
        entries = sorted(entries, key=lambda e: e[0])
        head = struct.pack("<H", len(entries))
        extra = b""
        extra_off = start + 2 + 12 * len(entries) + 4
        for tag, typ, vals in entries:
            b, count = encode(typ, vals)
            if len(b) <= 4:
                head += struct.pack("<HHI", tag, typ, count) + b.ljust(4, b"\0")
            else:
                head += struct.pack("<HHII", tag, typ, count, extra_off + len(extra))
                extra += b + (b"\0" if len(b) & 1 else b"")
        return head + struct.pack("<I", next_ifd) + extra

    blob = b"II" + struct.pack("<HI", 42, off_ifd0)
    blob += build(ifd0, off_ifd0)
    assert len(blob) == off_exif, (len(blob), off_exif)
    blob += build(exif, off_exif)
    blob = blob.ljust(off_data, b"\0")
    blob += data
    with open(path, "wb") as f:
        f.write(blob)


def expected_white(display):
    return display.white


def reference_display():
    return Display(cm.SRGB_TO_XYZ, gamma=2.2, luminance=1.0, black=0.001)
