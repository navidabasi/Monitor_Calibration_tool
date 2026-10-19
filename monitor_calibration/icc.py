"""Read a monitor's primaries and white point from an ICC display profile.

Works with matrix/shaper profiles (the kind colorimeter software and review
sites produce): it reads rXYZ/gXYZ/bXYZ and undoes the chromatic adaptation
to D50 that ICC applies, using the chad tag (v4, and v2.4+) or Bradford from
the media white point (older v2).
"""

import struct

import numpy as np

from . import colorimetry as cm

D50 = np.array([0.9642, 1.0, 0.8249])
BRADFORD = np.array([
    [0.8951, 0.2664, -0.1614],
    [-0.7502, 1.7135, 0.0367],
    [0.0389, -0.0685, 1.0296],
])


class ICCError(Exception):
    pass


def bradford(src, dst):
    """Adaptation matrix taking colors seen under white `src` to white `dst` (XYZ)."""
    scale = (BRADFORD @ dst) / (BRADFORD @ src)
    return np.linalg.inv(BRADFORD) @ np.diag(scale) @ BRADFORD


def _s15(b, i):
    return struct.unpack_from(">i", b, i)[0] / 65536.0


def _description(tag):
    if not tag or len(tag) < 12:
        return ""
    kind = tag[:4]
    if kind == b"desc":
        n = struct.unpack_from(">I", tag, 8)[0]
        return tag[12:12 + n].split(b"\0")[0].decode("latin-1", "replace").strip()
    if kind == b"mluc":
        count, size = struct.unpack_from(">II", tag, 8)
        if count and size >= 12:
            length, offset = struct.unpack_from(">II", tag, 16 + 4)
            return tag[offset:offset + length].decode("utf-16-be", "replace").strip("\0 ")
    return ""


def parse(data):
    """Return {name, primaries [(x, y)] * 3, white (x, y), cct, version}."""
    if len(data) < 132 or data[36:40] != b"acsp":
        raise ICCError("This isn't an ICC profile.")
    count = struct.unpack_from(">I", data, 128)[0]
    if count > 200 or 132 + 12 * count > len(data):
        raise ICCError("This ICC profile looks damaged.")
    tags = {}
    for i in range(count):
        sig, off, size = struct.unpack_from(">4sII", data, 132 + 12 * i)
        tags[sig] = data[off:off + size]

    def xyz(sig):
        t = tags.get(sig)
        if not t or len(t) < 20 or t[:4] != b"XYZ ":
            return None
        return np.array([_s15(t, 8 + 4 * j) for j in range(3)])

    colorants = [xyz(b"rXYZ"), xyz(b"gXYZ"), xyz(b"bXYZ")]
    if any(c is None for c in colorants):
        raise ICCError("This profile has no red/green/blue primaries (it's probably table-based). "
                       "Make a matrix/shaper profile, or enter the values by hand.")

    chad = tags.get(b"chad")
    if chad and chad[:4] == b"sf32" and len(chad) >= 44:
        undo = np.linalg.inv(np.array([_s15(chad, 8 + 4 * j) for j in range(9)]).reshape(3, 3))
        white = undo @ D50
    else:
        white = xyz(b"wtpt")
        if white is None or white[1] <= 0:
            white = D50.copy()
        undo = bradford(D50, white)
    primaries = [cm.xy(undo @ c) for c in colorants]
    white_xy = cm.xy(white)
    cct, _ = cm.cct_duv(cm.xy_to_xyz(white_xy))
    return {
        "name": _description(tags.get(b"desc")),
        "primaries": [[round(x, 4), round(y, 4)] for x, y in primaries],
        "white": [round(white_xy[0], 4), round(white_xy[1], 4)],
        "cct": round(cct),
        "version": f"{data[8]}.{data[9] >> 4}",
    }
