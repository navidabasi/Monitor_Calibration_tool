"""What may leave the computer: allow-lists and cleaning for everything shared.

Everything here is an allow-list. A field that isn't named is dropped, never
filtered by a block-list, so a new or unexpected tag (a serial number, a GPS
position, an owner name) can't slip through. The public description of these
fields is docs/DATA.md; keep both in step.

No photos are ever sent, only derived numbers. This module has no dependencies
so the hosted API can reuse it unchanged.
"""

import math
import re

SCREEN_TECHS = ("woled", "qdoled", "oled_rgb", "ips_ksf", "ips_wled", "va_qd", "va_wled", "mini_led_qd", "unknown")
PICTURE_MODES = ("srgb", "standard", "user", "native", "cinema", "game", "other")
REFERENCE_KINDS = ("srgb", "p3", "adobe", "icc", "manual", "device", "database")
RAW_TYPES = ("bayer", "linear")
APP_FAMILIES = ("lightroom", "apple", "open_camera", "samsung", "google", "other")

# Capture fields by name -> kind of value expected. Anything else is dropped.
CAPTURE_STRINGS = {
    "Make": 40, "Model": 60, "UniqueCameraModel": 80, "LensModel": 80, "Software": 60,
}
CAPTURE_NUMBERS = {
    "FocalLength", "FNumber", "ExposureTime", "ISOSpeedRatings", "WhiteLevel", "ImageWidth", "ImageLength",
    "Orientation", "CalibrationIlluminant1", "CalibrationIlluminant2",
}
CAPTURE_LISTS = {
    # name -> (maximum length, expected exact length or None)
    "DNGVersion": (4, 4), "CFAPattern": (16, None), "BlackLevel": (16, None),
    "ColorMatrix1": (9, 9), "ColorMatrix2": (9, 9), "ForwardMatrix1": (9, 9), "ForwardMatrix2": (9, 9),
    "AsShotNeutral": (3, 3),
}
CAPTURE_FIELDS = set(CAPTURE_STRINGS) | CAPTURE_NUMBERS | set(CAPTURE_LISTS) | {"RawType"}

_VERSION = re.compile(r"\d+(?:\.\d+){0,3}")


def _text(value, limit):
    """Printable ASCII only, trimmed. Text with a path separator or an address sign is dropped, not shortened."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"[^\x20-\x7e]", "", value).strip()
    if not text or "\\" in text or "@" in text:
        return None
    return text[:limit]


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def app_family(make, software):
    """Which kind of camera app wrote the file. The Software tag itself may carry more than that."""
    sw = (software or "").lower()
    maker = (make or "").lower()
    if "lightroom" in sw:
        return "lightroom"
    if "open camera" in sw or "opencamera" in sw:
        return "open_camera"
    if maker == "apple":
        return "apple"  # the iPhone camera's own ProRAW (Software is the iOS version)
    if maker == "samsung":
        return "samsung"
    if maker == "google":
        return "google"
    return "other"


def clean_capture(raw):
    """Keep only the allow-listed camera fields, with checked values. Software becomes "family version"."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for name, limit in CAPTURE_STRINGS.items():
        text = _text(raw.get(name), limit)
        if text is not None and name != "Software":
            out[name] = text
    # Software may hold anything, so only a known app name and a version number are kept.
    software = raw.get("Software") if isinstance(raw.get("Software"), str) else ""
    family = app_family(_text(raw.get("Make"), 40), software)
    version = _VERSION.search(software[:60])
    out["Software"] = f"{family} {version.group(0)}" if version else family
    for name in CAPTURE_NUMBERS:
        num = _number(raw.get(name))
        if num is not None:
            out[name] = num
    for name, (limit, exact) in CAPTURE_LISTS.items():
        vals = raw.get(name)
        if isinstance(vals, list) and 0 < len(vals) <= limit and (exact is None or len(vals) == exact):
            nums = [_number(v) for v in vals]
            if None not in nums:
                out[name] = nums
    if raw.get("RawType") in RAW_TYPES:
        out["RawType"] = raw["RawType"]
    return out


def phone_key(capture):
    """Who shares a correction: units of one phone model with the same lens, RAW type and camera app.

    Returns None when the file doesn't say what camera it came from.
    """
    cap = capture or {}
    model = cap.get("UniqueCameraModel") or cap.get("Model")
    if not model or cap.get("RawType") not in RAW_TYPES:
        return None
    lens = cap.get("LensModel") or (f"{cap['FocalLength']:.1f}mm" if cap.get("FocalLength") else "")
    return {
        "unique_camera_model": model[:80],
        "lens_model": lens[:80],
        "raw_type": cap["RawType"],
        "app_family": str(cap.get("Software", "other")).split(" ")[0],
    }


def phone_label(key):
    """A short description for the Results page."""
    if not key:
        return None
    model = re.sub(r"\s*(back|front)\s+camera.*$", "", key["unique_camera_model"], flags=re.I)
    return f"{model} · {key['app_family']} {key['raw_type']} RAW"


def clean_monitor(raw):
    """The fixed set of monitor fields. EDID serial numbers and user nicknames never get here; this drops them anyway."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    man = raw.get("manufacturer")
    if isinstance(man, str) and re.fullmatch(r"[A-Z]{3}", man):
        out["manufacturer"] = man
    code = raw.get("product_code")
    if isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 0xFFFF:
        out["product_code"] = code
    name = _text(raw.get("model_name"), 64)
    if name:
        out["model_name"] = name
    year = raw.get("year")
    if isinstance(year, int) and not isinstance(year, bool) and 1990 <= year <= 2100:
        out["year"] = year
    chroma = raw.get("chromaticity")
    if isinstance(chroma, dict):
        pts = {k: _number(chroma.get(k)) for k in ("rx", "ry", "gx", "gy", "bx", "by", "wx", "wy")}
        if all(v is not None and 0 <= v <= 1 for v in pts.values()):
            out["chromaticity"] = pts
    res = raw.get("resolution")
    if (isinstance(res, list) and len(res) == 2 and all(isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 20000 for v in res)):
        out["resolution"] = res
    if raw.get("screen_tech") in SCREEN_TECHS:
        out["screen_tech"] = raw["screen_tech"]
    if raw.get("picture_mode") in PICTURE_MODES:
        out["picture_mode"] = raw["picture_mode"]
    return out


def clean_settings(raw):
    """The monitor settings a person chose to share: whether they calibrated it, and the Red, Green and Blue
    values the monitor's menu showed (with the slider maximum). Returns None unless every value is sensible."""
    raw = raw if isinstance(raw, dict) else {}
    rgb = raw.get("rgb") if isinstance(raw.get("rgb"), dict) else {}
    vals = {k: _number(rgb.get(k)) for k in ("r", "g", "b", "max")}
    if not isinstance(raw.get("calibrated"), bool) or None in vals.values():
        return None
    if not (1 <= vals["max"] <= 1000 and all(0 < vals[k] <= vals["max"] for k in "rgb")):
        return None
    return {"calibrated": raw["calibrated"], "rgb": {k: round(float(v), 1) for k, v in vals.items()}}


def reference_kind(spec):
    """The user's choice of reference as one of the fixed kinds."""
    spec = spec or {}
    if spec.get("kind") == "custom":
        # "database": the values came from other people's profiles, so they must never be counted as a new measurement.
        return {"icc": "icc", "shared": "database"}.get(spec.get("source"), "manual")
    if spec.get("kind") == "device":
        return "device"
    return spec.get("name") if spec.get("name") in ("srgb", "p3", "adobe") else "srgb"


def clean_spec(spec):
    """The reference as the analyzer takes it: a preset name, or custom primaries and white point only
    (never an ICC profile's description text)."""
    spec = spec or {}
    if spec.get("kind") == "custom":
        try:
            prim = [[round(float(x), 5), round(float(y), 5)] for x, y in spec["primaries"]]
            white = [round(float(spec["white"][0]), 5), round(float(spec["white"][1]), 5)]
        except (KeyError, TypeError, ValueError):
            return {"name": "srgb"}
        if len(prim) != 3:
            return {"name": "srgb"}
        return {"kind": "custom", "primaries": prim, "white": white}
    return {"name": spec.get("name") if spec.get("name") in ("srgb", "p3", "adobe") else "srgb"}
