"""Chart layout shared by the web page and the photo analyzer.

Everything is in chart units. The chart is 100 x 75 (4:3, landscape) so it
fits the middle square of a phone camera's 3x3 grid exactly.

Layout:
  - four solid corner squares (fiducials) used to find the chart in a photo
  - a row of five ID bits along the top edge that say which chart this is
  - a 5 x 3 grid of patches separated by black gaps; the gaps let the
    analyzer measure lens glare next to every patch
"""

WIDTH = 100.0
HEIGHT = 75.0

FIDUCIAL_SIZE = 5.0
# Top-left corners of the corner squares, clockwise from top-left.
FIDUCIALS = [(0.0, 0.0), (95.0, 0.0), (95.0, 70.0), (0.0, 70.0)]
FIDUCIAL_CENTERS = [(x + FIDUCIAL_SIZE / 2, y + FIDUCIAL_SIZE / 2) for x, y in FIDUCIALS]

BIT_SIZE = 4.0
BITS_X = [34.0, 41.0, 48.0, 55.0, 62.0]
BITS_Y = 0.5

GRID_X = 7.0
GRID_Y = 9.0
COLS = 5
ROWS = 3
GAP = 4.0
PATCH_W = 14.0
PATCH_H = 55.0 / 3.0

CENTER_INDEX = 7  # middle patch; always white on the white and bright charts

TYPES = {"white": 1, "bright": 2, "dark": 3}
ROLES = {"reference": 0, "target": 1}

# Code value of the corner squares and lit ID bits. The dark chart uses a
# dim gray so its marks don't add glare to the near-black patches.
MARK_LEVEL = {"white": 255, "bright": 255, "dark": 64}


def _gray(key, code):
    return (key, (code, code, code))


# Row-major, top-left first. Red, green and blue sit next to the center white
# so the camera calibration uses patches close to the middle of the frame.
BRIGHT_PATCHES = [
    _gray("g20", 51), _gray("g30", 77), _gray("g40", 102), _gray("g50", 128), _gray("g60", 153),
    _gray("g70", 179), ("R", (255, 0, 0)), _gray("W", 255), ("G", (0, 255, 0)), _gray("g80", 204),
    _gray("g85", 217), _gray("g90", 230), ("B", (0, 0, 255)), _gray("g95", 242), _gray("W2", 255),
]

DARK_PATCHES = [
    _gray("d1", 3), _gray("d2", 5), _gray("d3", 8), _gray("d4", 10), _gray("d5", 13),
    _gray("d6", 15), _gray("d7", 18), _gray("d8", 20), _gray("d9", 23), _gray("d10", 26),
    _gray("d11", 28), _gray("d12", 31), _gray("d13", 33), _gray("d14", 36), _gray("d15", 38),
]

WHITE_PATCHES = [_gray(f"w{i}", 255) for i in range(COLS * ROWS)]

PATCHES = {"white": WHITE_PATCHES, "bright": BRIGHT_PATCHES, "dark": DARK_PATCHES}


def patch_rect(i):
    """Return (x, y, w, h) of patch i in chart units."""
    col, row = i % COLS, i // COLS
    return (
        GRID_X + col * (PATCH_W + GAP),
        GRID_Y + row * (PATCH_H + GAP),
        PATCH_W,
        PATCH_H,
    )


def bit_rect(i):
    return (BITS_X[i], BITS_Y, BIT_SIZE, BIT_SIZE)


def encode_bits(chart_type, role):
    """Five bits: start bit, 2-bit chart type, role, even parity."""
    t = TYPES[chart_type]
    b1, b2, b3 = (t >> 1) & 1, t & 1, ROLES[role]
    return [1, b1, b2, b3, (b1 + b2 + b3) % 2]


def decode_bits(bits):
    """Return (chart_type, role) or None if the bits aren't a valid code."""
    if len(bits) != 5 or bits[0] != 1:
        return None
    b1, b2, b3, parity = bits[1:]
    if (b1 + b2 + b3) % 2 != parity:
        return None
    t = (b1 << 1) | b2
    chart_type = next((k for k, v in TYPES.items() if v == t), None)
    if chart_type is None:
        return None
    role = "target" if b3 else "reference"
    return chart_type, role


def layout_json():
    """Everything the web page needs to draw the charts."""
    return {
        "width": WIDTH,
        "height": HEIGHT,
        "fiducialSize": FIDUCIAL_SIZE,
        "fiducials": FIDUCIALS,
        "bits": [bit_rect(i) for i in range(len(BITS_X))],
        "patches": [patch_rect(i) for i in range(COLS * ROWS)],
        "charts": {
            name: {"colors": [rgb for _, rgb in patches], "keys": [k for k, _ in patches], "mark": MARK_LEVEL[name]}
            for name, patches in PATCHES.items()
        },
        "codes": {f"{t}:{r}": encode_bits(t, r) for t in TYPES for r in ROLES},
    }
