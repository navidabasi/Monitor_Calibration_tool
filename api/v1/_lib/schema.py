"""Strict validation of everything the API accepts.

Unknown fields are rejected, numbers must be finite and inside their limits,
strings and arrays are bounded, and the result is a new dict built only from
validated values. The field lists mirror the allow-list in
monitor_calibration/privacy.py and docs/DATA.md.
"""

import math
import re

from monitor_calibration import chart, privacy

MAX_DEPTH_KEYS = 40


class Invalid(Exception):
    """The request doesn't match the schema. The message is for tests and logs; clients get a generic error."""


# ---------------------------------------------------------------- building blocks

def num(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise Invalid(f"number out of range [{lo}, {hi}]")
    return v


def integer(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise Invalid(f"integer out of range [{lo}, {hi}]")
    return v


def string(v, max_len, pattern=None, min_len=0):
    if not isinstance(v, str) or not min_len <= len(v) <= max_len:
        raise Invalid("bad string length")
    if pattern is not None and not re.fullmatch(pattern, v):
        raise Invalid("bad string format")
    return v


def enum(v, options):
    if not isinstance(v, str) or v not in options:
        raise Invalid("not an allowed value")
    return v


def boolean(v):
    if not isinstance(v, bool):
        raise Invalid("expected true or false")
    return v


def array(v, item, min_len, max_len):
    if not isinstance(v, list) or not min_len <= len(v) <= max_len:
        raise Invalid("bad array length")
    return [item(x) for x in v]


def nums(lo, hi, min_len, max_len=None):
    return lambda v: array(v, lambda x: num(x, lo, hi), min_len, max_len if max_len is not None else min_len)


def obj(v, required, optional=None):
    """A dict with exactly these keys (required ones must be there). `required` and `optional` map key -> validator."""
    optional = optional or {}
    if not isinstance(v, dict) or len(v) > MAX_DEPTH_KEYS:
        raise Invalid("expected an object")
    extra = set(v) - set(required) - set(optional)
    if extra:
        raise Invalid("unknown field")
    out = {}
    for key, check in required.items():
        if key not in v:
            raise Invalid(f"missing field {key}")
        out[key] = check(v[key])
    for key, check in optional.items():
        if key in v and v[key] is not None:
            out[key] = check(v[key])
    return out


def nullable(check):
    return lambda v: None if v is None else check(v)


HEX32 = r"[0-9a-f]{32}"
HEX64 = r"[0-9a-f]{64}"

# ---------------------------------------------------------------- submission parts

CHALLENGE = lambda v: obj(v, {  # noqa: E731
    "nonce": lambda x: string(x, 32, HEX32, 32),
    "difficulty": lambda x: integer(x, 1, 40),
    "expires": lambda x: integer(x, 0, 4_000_000_000),
    "sig": lambda x: string(x, 64, HEX64, 64),
    "counter": lambda x: integer(x, 0, 2**63 - 1),
})

SOFTWARE = r"(?:" + "|".join(privacy.APP_FAMILIES) + r")(?: \d+(?:\.\d+){0,3})?"


def capture(v):
    optional = {}
    for name, limit in privacy.CAPTURE_STRINGS.items():
        optional[name] = (lambda x, lim=limit: string(x, lim, r"[\x20-\x7e]*")) if name != "Software" else (lambda x: string(x, 60, SOFTWARE))
    for name in privacy.CAPTURE_NUMBERS:
        optional[name] = lambda x: num(x, -1e9, 1e9)
    for name, (limit, exact) in privacy.CAPTURE_LISTS.items():
        optional[name] = nums(-1e6, 1e6, exact or 1, limit)
    optional["RawType"] = lambda x: enum(x, privacy.RAW_TYPES)
    out = obj(v, {"UniqueCameraModel": lambda x: string(x, 80, r"[\x20-\x7e]+", 1), "RawType": optional.pop("RawType"),
                  "Software": optional.pop("Software"), "ColorMatrix1": optional.pop("ColorMatrix1")}, optional)
    if "\\" in out["UniqueCameraModel"] or "@" in out["UniqueCameraModel"]:
        raise Invalid("not a camera name")
    return out


def monitor(v, need_tech):
    required = {"screen_tech": lambda x: enum(x, privacy.SCREEN_TECHS)} if need_tech else {}
    optional = {
        "manufacturer": lambda x: string(x, 3, r"[A-Z]{3}", 3),
        "product_code": lambda x: integer(x, 0, 65535),
        "model_name": lambda x: string(x, 64, r"[\x20-\x7e]+", 1),
        "year": lambda x: integer(x, 1991, 2100),
        "chromaticity": lambda x: obj(x, {k: (lambda y: num(y, 0, 1)) for k in ("rx", "ry", "gx", "gy", "bx", "by", "wx", "wy")}),
        "resolution": lambda x: array(x, lambda y: integer(y, 100, 20000), 2, 2),
        "picture_mode": lambda x: enum(x, privacy.PICTURE_MODES),
    }
    if not need_tech:
        optional["screen_tech"] = lambda x: enum(x, privacy.SCREEN_TECHS)
    return obj(v, required, optional)


def spec(v):
    xy = nums(0, 1, 2)
    if isinstance(v, dict) and "name" in v:
        return obj(v, {"name": lambda x: enum(x, ("srgb", "p3", "adobe"))})
    return obj(v, {"kind": lambda x: enum(x, ("custom",)), "primaries": lambda x: array(x, xy, 3, 3), "white": xy})


PATCH = lambda v: obj(v, {  # noqa: E731
    "pos": nums(-1, 2, 2), "signal": nums(-1e4, 1e4, 3), "flare": nums(-1e4, 1e4, 3),
    "noise": lambda x: num(x, 0, 1e4), "n": lambda x: integer(x, 0, 10**9), "clipped": boolean,
})

MARK = lambda v: obj(v, {"pos": nums(-1, 2, 2), "signal": nums(-1e4, 1e4, 3)})  # noqa: E731

CHART = lambda v: obj(v, {  # noqa: E731
    "type": lambda x: enum(x, tuple(chart.TYPES)),
    "role": lambda x: enum(x, tuple(chart.ROLES)),
    "size": lambda x: array(x, lambda y: integer(y, 100, 100000), 2, 2),
    "framing": lambda x: obj(x, {"fraction": lambda y: num(y, 0, 2), "keystone": lambda y: num(y, 0.5, 20),
                                 "angle": lambda y: num(y, -360, 360), "offset": lambda y: num(y, 0, 5)}),
    "exposure": lambda x: obj(x, {"shutter": lambda y: num(y, 0, 3600), "iso": lambda y: num(y, 0, 1e7),
                                  "norm": lambda y: num(y, 0, 1e9)}),
    "peak": lambda x: num(x, 0, 100),
    "cam_to_xyz": nullable(nums(-1e4, 1e4, 9)),
    "patches": lambda x: array(x, PATCH, chart.COLS * chart.ROWS, chart.COLS * chart.ROWS),
    "marks": lambda x: array(x, MARK, 0, 4),
})

SUMMARY = lambda v: obj(v, {  # noqa: E731
    "a_ref": nums(-1e6, 1e6, 9),
    "ref_additivity": lambda x: num(x, 0, 100),
    "ref_gamma": lambda x: num(x, 0, 10),
    "ref_max_tint": lambda x: num(x, 0, 1000),
    "round_cct": nullable(lambda x: num(x, 0, 1e6)),
    "round_gamma": nullable(lambda x: num(x, 0, 10)),
})


SETTINGS = lambda v: obj(v, {  # noqa: E731
    "calibrated": boolean,
    "rgb": lambda x: obj(x, {"r": lambda y: num(y, 0.1, 1000), "g": lambda y: num(y, 0.1, 1000),
                             "b": lambda y: num(y, 0.1, 1000), "max": lambda y: num(y, 1, 1000)}),
})


def submission(v):
    """Validate a POST /submissions body. Returns a new dict with only the known, checked fields."""
    out = obj(v, {
        "v": lambda x: integer(x, 1, 1),
        "challenge": CHALLENGE,
        "deletion_hash": lambda x: string(x, 64, HEX64, 64),
        "app_version": lambda x: string(x, 20, r"[0-9A-Za-z.+-]+", 1),
        "analysis_version": lambda x: integer(x, 1, 1000),
        "session_month": lambda x: string(x, 7, r"20\d\d-(?:0[1-9]|1[0-2])", 7),
        "capture": capture,
        "reference_monitor": lambda x: monitor(x, need_tech=True),
        "reference": lambda x: obj(x, {"kind": lambda y: enum(y, privacy.REFERENCE_KINDS), "spec": spec}),
        "charts": lambda x: array(x, CHART, 2, 3),
        "summary": SUMMARY,
    }, {
        "turnstile": lambda x: string(x, 2048),
        "target_monitor": lambda x: monitor(x, need_tech=False),
        "settings": SETTINGS,
    })
    seen = [(c["type"], c["role"]) for c in out["charts"]]
    if len(set(seen)) != len(seen) or ("bright", "reference") not in seen or ("white", "target") not in seen:
        raise Invalid("charts must include a reference shot and a white chart, once each")
    if "settings" in out:
        rgb = out["settings"]["rgb"]
        tgt = out.get("target_monitor", {})
        if not all(0 < rgb[k] <= rgb["max"] for k in "rgb"):
            raise Invalid("slider values above the slider maximum")
        if not (tgt.get("manufacturer") and "product_code" in tgt and tgt.get("picture_mode")):
            raise Invalid("settings need a named monitor model and a picture mode")
        if ("bright", "target") not in seen:
            raise Invalid("settings need the bright chart of the monitor")
    return out


def delete_request(v):
    return obj(v, {"code": lambda x: string(x, 32, HEX32, 32)})


PHONE_QUERY = {
    "unique_camera_model": lambda x: string(x, 80, r"[\x20-\x7e]+", 1),
    "lens_model": lambda x: string(x, 80, r"[\x20-\x7e]*"),
    "raw_type": lambda x: enum(x, privacy.RAW_TYPES),
    "app_family": lambda x: enum(x, privacy.APP_FAMILIES),
}


def corrections_query(params):
    """Query string values (a dict of str -> str) for GET /corrections."""
    return obj(params, PHONE_QUERY, {
        "monitor": lambda x: string(x, 12, r"[A-Z]{3}:\d{1,5}"),
        "tech": lambda x: enum(x, privacy.SCREEN_TECHS),
    })


def recipe_query(params):
    return obj(params, {
        "monitor": lambda x: string(x, 12, r"[A-Z]{3}:\d{1,5}"),
        "mode": lambda x: enum(x, privacy.PICTURE_MODES),
    })


def search_query(params):
    return obj(params, {"q": lambda x: string(x, 40, r"[\x20-\x7e]+", 3)})
