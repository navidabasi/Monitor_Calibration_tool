"""Read selected tags straight from a TIFF-based RAW file (DNG and most camera RAW formats).

Reading the tags here works with every rawpy version (older ones don't expose
them) and touches only tags the app needs:

  read_exposure()  shutter, ISO and date, for the analysis
  read_capture()   the camera and capture fields that may be shared (the
                   allow-list in CAPTURE_TAGS). Tags are looked up by ID, so a
                   serial number, GPS position, owner name or MakerNote is
                   never even read.
"""

import struct
import time

EXPOSURE_TIME = 33434
ISO_SPEED = 34855
DATETIME_ORIGINAL = 36867
EXIF_IFD = 34665
SUB_IFDS = 330
PHOTOMETRIC = 262
TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
MAX_ENTRIES = 1000
MAX_IFDS = 64
MAX_VALUE_BYTES = 4096

# The only tags read_capture() returns. Never add a tag here without adding it
# to docs/DATA.md and the allow-list in privacy.py.
CAPTURE_TAGS = {
    271: "Make",
    272: "Model",
    50708: "UniqueCameraModel",
    42036: "LensModel",
    37386: "FocalLength",
    33437: "FNumber",
    33434: "ExposureTime",
    34855: "ISOSpeedRatings",
    305: "Software",
    50706: "DNGVersion",
    33422: "CFAPattern",
    50714: "BlackLevel",
    50717: "WhiteLevel",
    50721: "ColorMatrix1",
    50722: "ColorMatrix2",
    50964: "ForwardMatrix1",
    50965: "ForwardMatrix2",
    50778: "CalibrationIlluminant1",
    50779: "CalibrationIlluminant2",
    50728: "AsShotNeutral",
    256: "ImageWidth",
    257: "ImageLength",
    274: "Orientation",
}
# Fields that are lists of numbers; every other field is a single value.
LIST_FIELDS = {"DNGVersion", "CFAPattern", "BlackLevel", "ColorMatrix1", "ColorMatrix2", "ForwardMatrix1",
               "ForwardMatrix2", "AsShotNeutral"}
RAW_PHOTOMETRIC = {32803: "bayer", 34892: "linear"}  # CFA, LinearRaw
# Tags that describe the RAW image itself: taken from the RAW IFD, not from a preview.
RAW_IMAGE_TAGS = {256, 257, 274, 33422, 50714, 50717}


def read_exposure(path):
    """Return {"shutter", "iso", "timestamp"} (0.0 when missing), or None if it isn't a TIFF file."""
    with open(path, "rb") as f:
        walked = _walk(f)
        if walked is None:
            return None
        end, ifds = walked
        found = {}
        for entries in ifds:
            for tag in (EXPOSURE_TIME, ISO_SPEED, DATETIME_ORIGINAL):
                if tag in entries and tag not in found:
                    found[tag] = entries[tag]

        shutter = iso = stamp = 0.0
        if EXPOSURE_TIME in found:
            vals = _values(f, end, *found[EXPOSURE_TIME])
            shutter = float(vals[0]) if vals else 0.0
        if ISO_SPEED in found:
            vals = _values(f, end, *found[ISO_SPEED])
            iso = float(vals[0]) if vals else 0.0
        if DATETIME_ORIGINAL in found:
            text = _values(f, end, *found[DATETIME_ORIGINAL])
            try:
                stamp = time.mktime(time.strptime(text.strip(), "%Y:%m:%d %H:%M:%S"))
            except (ValueError, OverflowError, AttributeError):
                stamp = 0.0
    return {"shutter": shutter, "iso": iso, "timestamp": stamp}


def read_capture(path):
    """The allow-listed camera fields of a TIFF-based RAW file, or None if it isn't one.

    Values are raw (numbers, lists, strings). privacy.clean_capture() checks and
    trims them before anything is shown or sent.
    """
    with open(path, "rb") as f:
        walked = _walk(f)
        if walked is None:
            return None
        end, ifds = walked
        raw_ifd = None
        for entries in ifds:
            if PHOTOMETRIC in entries:
                vals = _values(f, end, *entries[PHOTOMETRIC])
                if isinstance(vals, list) and vals and vals[0] in RAW_PHOTOMETRIC:
                    raw_ifd = entries
                    break
        ordered = ([raw_ifd] if raw_ifd else []) + [e for e in ifds if e is not raw_ifd]

        out = {}
        for entries in ordered:
            for tag, name in CAPTURE_TAGS.items():
                if tag in entries and name not in out:
                    if tag in RAW_IMAGE_TAGS and raw_ifd is not None and entries is not raw_ifd:
                        continue  # a preview's size or black level isn't the RAW's
                    value = _values(f, end, *entries[tag])
                    if isinstance(value, list) and name not in LIST_FIELDS and value:
                        value = value[0]
                    if value != [] and value != "":
                        out[name] = value
        if raw_ifd is not None:
            kind = _values(f, end, *raw_ifd[PHOTOMETRIC])
            out["RawType"] = RAW_PHOTOMETRIC[kind[0]]
    return out


def _walk(f):
    """(byte order, [tag -> (type, count, raw) for every IFD]) in reading order, or None if not a TIFF."""
    head = f.read(8)
    if head[:4] == b"II*\0":
        end = "<"
    elif head[:4] == b"MM\0*":
        end = ">"
    else:
        return None
    ifds = []
    pending = [struct.unpack(end + "I", head[4:8])[0]]
    seen = set()
    while pending and len(seen) < MAX_IFDS:
        off = pending.pop()
        if off == 0 or off in seen:
            continue
        seen.add(off)
        entries = {}
        for tag, typ, count, raw in _entries(f, off, end):
            if tag in (EXIF_IFD, SUB_IFDS):
                pending.extend(v for v in _values(f, end, typ, count, raw) if isinstance(v, int))
            elif tag not in entries:
                entries[tag] = (typ, count, raw)
        ifds.append(entries)
        pending.extend(n for n in [_next_ifd(f, off, end)] if n)
    return end, ifds


def _entries(f, off, end):
    f.seek(off)
    data = f.read(2)
    if len(data) < 2:
        return []
    n = struct.unpack(end + "H", data)[0]
    if n > MAX_ENTRIES:
        return []
    block = f.read(12 * n)
    return [struct.unpack_from(end + "HHI4s", block, 12 * i) for i in range(len(block) // 12)]


def _next_ifd(f, off, end):
    f.seek(off)
    data = f.read(2)
    if len(data) < 2:
        return 0
    n = struct.unpack(end + "H", data)[0]
    f.seek(off + 2 + 12 * n)
    data = f.read(4)
    return struct.unpack(end + "I", data)[0] if len(data) == 4 else 0


def _values(f, end, typ, count, raw):
    size = TYPE_SIZES.get(typ, 1) * count
    if size > MAX_VALUE_BYTES:
        return []
    if size <= 4:
        data = raw[:size]
    else:
        f.seek(struct.unpack(end + "I", raw)[0])
        data = f.read(size)
    if len(data) < size:
        return []
    if typ == 2:
        return data.split(b"\0")[0].decode("ascii", "replace")
    if typ in (1, 7):
        return list(data)
    if typ == 3:
        return list(struct.unpack(end + f"{count}H", data))
    if typ in (4, 13):
        return list(struct.unpack(end + f"{count}I", data))
    if typ == 9:
        return list(struct.unpack(end + f"{count}i", data))
    if typ == 11:
        return list(struct.unpack(end + f"{count}f", data))
    if typ == 12:
        return list(struct.unpack(end + f"{count}d", data))
    if typ in (5, 10):
        nums = struct.unpack(end + f"{2 * count}{'I' if typ == 5 else 'i'}", data)
        return [nums[i] / nums[i + 1] if nums[i + 1] else 0.0 for i in range(0, len(nums), 2)]
    return []
