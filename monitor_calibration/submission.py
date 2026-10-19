"""What a shared session looks like, and how the server checks it.

The local app builds a submission from measured numbers (never photos). The
hosted API rebuilds the analysis from those numbers with the same code, and
only accepts the session if its own result matches the client's summary and
is physically plausible. The client is untrusted, so nothing the client
computed is believed; the summary exists only to catch edited payloads.

numpy only (no scipy or rawpy), so the API bundle stays small.
"""

import math

import numpy as np

from . import analyze, chart, privacy
from . import colorimetry as cm

SCHEMA_VERSION = 1
MAX_CHARTS = 3
SHARED_SHOTS = (("bright", "reference"), ("white", "target"), ("bright", "target"))

# How closely the server's recomputed matrix must match the client's summary.
SUMMARY_TOLERANCE = 2e-3


# ---------------------------------------------------------------- client side

def _r(v, digits=6):
    """Round a float to 6 significant digits; keeps the payload small and the client and server in step."""
    v = float(v)
    return float(f"{v:.{digits}g}") if math.isfinite(v) else 0.0


def _rl(values):
    return [_r(v) for v in values]


def scrub_chart(photo):
    """The shareable numbers of one measured photo (detect.measure output). No raw pixels, time or warnings."""
    res = photo["result"]
    ident = analyze.identity(photo)
    return {
        "type": ident[0],
        "role": ident[1],
        "size": [int(res["size"][0]), int(res["size"][1])],
        "framing": {k: _r(res[name]) for k, name in (("fraction", "chartFraction"), ("keystone", "keystone"),
                                                      ("angle", "angle"), ("offset", "offset"))},
        "exposure": {"shutter": _r(res["exposure"]["shutter"]), "iso": _r(res["exposure"]["iso"]),
                     "norm": _r(res["exposure"]["norm"])},
        "peak": _r(res.get("peak", 0)),
        "cam_to_xyz": [_r(v) for row in res["camToXyz"] for v in row] if res.get("camToXyz") else None,
        "patches": [{"pos": _rl(p["pos"]), "signal": _rl(p["signal"]), "flare": _rl(p["flare"]),
                     "noise": _r(p["noise"]), "n": int(p["n"]), "clipped": bool(p["clipped"])} for p in res["patches"]],
        "marks": [{"pos": _rl(m["pos"]), "signal": _rl(m["signal"])} for m in res.get("marks", [])],
    }


def pick_photos(photos):
    """The photos to share: the newest of each shot in SHARED_SHOTS."""
    usable = [p for p in photos if p.get("result") and analyze.identity(p)]
    usable.sort(key=lambda p: p["id"])
    out = []
    for shot in SHARED_SHOTS:
        found = [p for p in usable if analyze.identity(p) == shot]
        if found:
            out.append(found[-1])
    return out


def photos_from_charts(charts):
    """Rebuild the photo dicts analyze_session() works on."""
    photos = []
    for i, c in enumerate(charts, start=1):
        fr, ex = c["framing"], c["exposure"]
        photos.append({
            "id": i,
            "name": f"chart{i}",
            "result": {
                "chart": {"type": c["type"], "role": c["role"]},
                "size": c["size"],
                "chartFraction": fr["fraction"], "keystone": fr["keystone"], "angle": fr["angle"], "offset": fr["offset"],
                "exposure": ex,
                "timestamp": 0.0,
                "camToXyz": [c["cam_to_xyz"][0:3], c["cam_to_xyz"][3:6], c["cam_to_xyz"][6:9]] if c.get("cam_to_xyz") else None,
                "peak": c.get("peak", 0.0),
                "patches": [dict(p, index=j, raw=[0, 0, 0]) for j, p in enumerate(c["patches"])],
                "marks": c.get("marks", []),
                "warnings": [],
            },
        })
    return photos


def summarize(analysis):
    """The client's summary of its own result, for the server to compare against. None if not a reference session."""
    corr, ref = analysis.get("correction"), analysis.get("reference")
    if analysis.get("method") != "reference" or not corr or not ref:
        return None
    rnd = analysis["rounds"][-1] if analysis.get("rounds") else None
    return {
        "a_ref": [_r(v) for row in corr["aRef"] for v in row],
        "ref_additivity": _r(ref["additivity"]),
        "ref_gamma": _r(ref["gamma"] or 0),
        "ref_max_tint": _r(ref["maxTint"]),
        "round_cct": _r(rnd["white"]["cct"]) if rnd else None,
        "round_gamma": _r(rnd["gamma"]) if rnd and rnd["gamma"] else None,
    }


def analyze_shared(charts, spec):
    """Run the analysis on shared charts, as the client and the server both do."""
    spec = spec or {"name": "srgb"}
    return analyze.analyze_session(photos_from_charts(charts), "2.2", {}, spec)


def build(photos, spec, need_round=False):
    """(charts, summary, analysis) for a session worth sharing, or None if it has no good reference shot.

    With need_round the session must also have a bright chart on the monitor being calibrated, because shared
    monitor settings are judged by how that chart measured."""
    chosen = pick_photos(photos)
    if not any(analyze.identity(p) == ("bright", "reference") for p in chosen):
        return None
    if need_round and not any(analyze.identity(p) == ("bright", "target") for p in chosen):
        return None
    charts = [scrub_chart(p) for p in chosen]
    analysis = analyze_shared(charts, spec)
    summary = summarize(analysis)
    if summary is None or not analysis["correction"] or not analysis["correction"]["good"]:
        return None
    return charts, summary, analysis


# ---------------------------------------------------------------- server side

class Rejected(Exception):
    """The submission is well-formed but doesn't hold up. The message is for logs and tests, not for the client."""


def verify(charts, spec, summary, capture):
    """Recompute everything from the raw readings and check it. Returns the server's own result dict.

    Raises Rejected when the client's summary disagrees with the recomputation
    (an edited payload) or the session isn't physically plausible.
    """
    analysis = analyze_shared(charts, spec)
    corr = analysis.get("correction")
    if analysis.get("method") != "reference" or not corr or not analysis.get("reference"):
        raise Rejected("no usable reference shot")
    if not corr["good"]:
        raise Rejected("poor quality: " + "; ".join(corr["problems"]))
    ref = analysis["reference"]

    # 1. The client's numbers must be the ones the readings give.
    a_ref = np.array(corr["aRef"], dtype=float).ravel()
    claimed = np.array(summary["a_ref"], dtype=float)
    if not np.allclose(a_ref, claimed, rtol=SUMMARY_TOLERANCE, atol=SUMMARY_TOLERANCE * np.abs(a_ref).max()):
        raise Rejected("summary does not match the readings (matrix)")
    for key, mine in (("ref_additivity", ref["additivity"]), ("ref_gamma", ref["gamma"]), ("ref_max_tint", ref["maxTint"])):
        if abs(summary[key] - mine) > 0.01 + 0.01 * abs(mine):
            raise Rejected(f"summary does not match the readings ({key})")
    rnd = analysis["rounds"][-1] if analysis["rounds"] else None
    if summary.get("round_cct") is not None:
        if rnd is None or abs(summary["round_cct"] - rnd["white"]["cct"]) > 5 + 0.002 * rnd["white"]["cct"]:
            raise Rejected("summary does not match the readings (white point)")

    checks = plausibility(charts, analysis, capture)
    if checks["failed"]:
        raise Rejected("implausible: " + ", ".join(checks["failed"]))

    # The last bright chart of the target monitor, as the server measured it (what shared settings are judged by).
    rnd_out = None
    if rnd:
        rnd_out = {"cct": rnd["white"]["cct"], "duv": rnd["white"]["duv"], "dE": rnd["white"]["dE"],
                   "gamma": rnd["gamma"], "max_tint": rnd["maxTint"]}
    return {"a_ref": corr["aRef"], "k": corr["k"], "a_cam": corr["aCam"], "white_cam": corr["whiteCam"], "round": rnd_out,
            "checks": checks, "version": SCHEMA_VERSION}


def plausibility(charts, analysis, capture):
    """Physical sanity checks (plan section 8). Returns {"passed": [...], "failed": [...]}."""
    passed, failed = [], []

    def check(name, ok):
        (passed if ok else failed).append(name)

    corr, ref = analysis["correction"], analysis["reference"]
    photos = {(c["type"], c["role"]): c for c in charts}
    ref_chart = photos[("bright", "reference")]

    # Patch signals positive, gray ramp ordered, nothing clipped.
    check("signals_positive", all(min(p["signal"]) > 0 for c in charts if c["type"] != "dark" for p in c["patches"]))
    grays = sorted((s for s in ref["steps"]), key=lambda s: s["level"])
    check("gray_ramp_ordered", all(b["Y"] > a["Y"] for a, b in zip(grays, grays[1:])))
    check("no_clipping", not any(p["clipped"] for c in charts for p in c["patches"]))

    # The reference monitor behaves like a decent monitor.
    check("reference_additivity", ref["additivity"] < 0.06)
    check("reference_gamma", ref["gamma"] is not None and 1.9 <= ref["gamma"] <= 2.6)
    check("reference_tint", ref["maxTint"] < 4)

    # Framing.
    check("chart_fraction", all(0.2 <= c["framing"]["fraction"] <= 0.45 for c in charts))
    check("keystone", all(c["framing"]["keystone"] < 1.1 for c in charts))

    # The matrix is well-conditioned and the correction isn't absurd next to the maker's data.
    a_ref, a_cam, white = np.array(corr["aRef"]), np.array(corr["aCam"]), np.array(corr["whiteCam"])
    check("matrix_conditioned", bool(np.isfinite(a_ref).all() and np.linalg.cond(a_ref) < 50))
    check("correction_sane", analyze.valid_correction(corr["k"]))
    try:
        cct_r, duv_r = cm.cct_duv(a_ref @ white)
        cct_c, duv_c = cm.cct_duv(a_cam @ white)
        check("white_vs_maker_matrix", abs(cct_r - cct_c) <= 2000 and abs(duv_r - duv_c) <= 0.02)
    except (ValueError, FloatingPointError):
        check("white_vs_maker_matrix", False)

    # Exposure normalization follows shutter x ISO, and the file's own exposure tags agree.
    def norm_ok(c):
        ex = c["exposure"]
        return ex["shutter"] > 0 and ex["iso"] > 0 and abs(ex["norm"] - ex["shutter"] * ex["iso"] / 100.0) <= 0.01 * ex["norm"] + 1e-9
    check("exposure_norm", all(norm_ok(c) for c in charts))
    cap_t, cap_iso = capture.get("ExposureTime"), capture.get("ISOSpeedRatings")
    ex = ref_chart["exposure"]
    check("exposure_matches_tags", bool(cap_t and cap_iso and abs(cap_t - ex["shutter"]) <= 0.02 * cap_t
                                        and abs(cap_iso - ex["iso"]) <= 0.02 * cap_iso))

    # All charts come from the same camera data.
    mats = [np.array(c["cam_to_xyz"]) for c in charts if c.get("cam_to_xyz")]
    check("same_camera_matrix", len(mats) == len(charts) and all(np.allclose(m, mats[0], rtol=1e-3, atol=1e-6) for m in mats))
    return {"passed": passed, "failed": failed}


def normalized_correction(server_result):
    """The correction to aggregate for this submission."""
    return np.array(server_result["k"], dtype=float)


def d65_probe(k):
    """(cct, duv) of a D65 screen white seen through correction k: the 'size' of the correction for comparisons."""
    return analyze.white_after_correction(k)


def known_charts():
    """Chart type and role names the schema accepts."""
    return tuple(chart.TYPES), tuple(chart.ROLES)


def spec_from_payload(reference):
    """The analyzer's reference spec from a submission's `reference` block."""
    return privacy.clean_spec(reference.get("spec"))
