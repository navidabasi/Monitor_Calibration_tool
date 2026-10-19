"""Turn measured photos into calibration results and advice.

Shots:
  reference / bright  calibrates the camera against a monitor with known colors
  target / white      maps lens falloff across the frame (flat field)
  target / bright     gamma, gray tint, white point, colors (one per adjustment round)
  target / dark       near-black detail
"""

import math

import numpy as np

from . import chart
from . import colorimetry as cm

BRIGHT_INDEX = {key: i for i, (key, _) in enumerate(chart.BRIGHT_PATCHES)}
COLOR_NAMES = {0: ("red", "cyan"), 1: ("green", "magenta"), 2: ("blue", "yellow")}

D65_XY = (0.3127, 0.3290)
PRESETS = {
    "srgb": {"name": "sRGB", "primaries": [(0.64, 0.33), (0.30, 0.60), (0.15, 0.06)], "white": D65_XY},
    "p3": {"name": "Display P3", "primaries": [(0.680, 0.320), (0.265, 0.690), (0.150, 0.060)], "white": D65_XY},
    "adobe": {"name": "Adobe RGB", "primaries": [(0.64, 0.33), (0.21, 0.71), (0.15, 0.06)], "white": D65_XY},
}


def reference_spec(spec):
    """What the reference monitor shows: a preset name or custom primaries/white (xy).

    Falls back to sRGB if the spec is missing or not physically sensible.
    """
    spec = spec or {}
    if spec.get("kind") == "custom":
        try:
            prim = [(float(x), float(y)) for x, y in spec["primaries"]]
            white = (float(spec["white"][0]), float(spec["white"][1]))
            pts = prim + [white]
            if len(prim) == 3 and all(0 < x < 0.8 and 0 < y < 0.9 for x, y in pts) and cm.triangle_area(prim) > 0.02:
                return {"name": str(spec.get("name") or "custom values")[:80], "primaries": prim, "white": white}
        except (KeyError, TypeError, ValueError):
            pass
        return dict(PRESETS["srgb"], invalid=True)
    return PRESETS.get(spec.get("name"), PRESETS["srgb"])


def identity(photo):
    """(type, role) for a photo, preferring a manual assignment."""
    a = photo.get("assigned")
    if a:
        return a["type"], a["role"]
    c = (photo.get("result") or {}).get("chart")
    return (c["type"], c["role"]) if c else None


def _sig(patch, corr):
    return np.array(patch["signal"], dtype=float) * corr(patch["pos"])


def _design(pos):
    pos = np.atleast_2d(pos)
    x, y = pos[:, 0], pos[:, 1]
    return np.c_[np.ones_like(x), x, y, x * x, x * y, y * y]


def _flat_field(white_photo):
    """Per-channel lens falloff model from the all-white chart.

    Returns corr(pos) giving the factor that brings a patch at pos to what it
    would read at the chart center, and the worst fit residual.
    """
    patches = white_photo["result"]["patches"]
    pos = np.array([p["pos"] for p in patches])
    vals = np.array([p["signal"] for p in patches])
    if (vals <= 0).any():
        return None, None
    coef, *_ = np.linalg.lstsq(_design(pos), vals, rcond=None)
    f0 = (_design(pos[chart.CENTER_INDEX]) @ coef)[0]
    resid = float(np.abs(vals / (_design(pos) @ coef) - 1).max())

    def corr(p):
        f = (_design(p) @ coef)[0]
        return np.where(f > 0, f0 / f, 1.0)

    return corr, resid


def _no_corr(_pos):
    return np.ones(3)


def _reference_matrix(ref_photo, corr, spec):
    """Camera RGB -> XYZ, assuming the reference monitor shows exactly `spec`.

    XYZ comes out relative to the reference white (Y = 1).
    """
    p = ref_photo["result"]["patches"]
    cam = np.column_stack([_sig(p[BRIGHT_INDEX[k]], corr) for k in ("R", "G", "B", "W")])
    m_ref = cm.rgb_to_xyz_matrix(spec["primaries"], spec["white"])
    xyz = np.column_stack([m_ref[:, 0], m_ref[:, 1], m_ref[:, 2], m_ref.sum(axis=1)])
    w = np.array([1.0, 1.0, 1.0, 3.0])  # white matters most
    m = (cam * w) @ xyz.T @ np.linalg.inv((xyz * w) @ xyz.T)
    additivity = float(np.linalg.norm(cam[:, 3] - cam[:, :3].sum(axis=1)) / np.linalg.norm(cam[:, 3]))
    return np.linalg.inv(m), additivity


def _camera_matrix(photo):
    m = photo["result"].get("camToXyz")
    return np.array(m, dtype=float) if m else None


MAX_CORRECTION_COND = 20.0


def correction_matrix(a_ref, a_cam, white_cam):
    """The phone's error on this screen's light, as a matrix on top of its own color data.

    a_ref turns camera RGB into XYZ for the screen that was measured; a_cam is
    the phone maker's matrix from the DNG. K = a_ref . inv(a_cam), scaled so it
    keeps the luminance of white. Unlike a_ref, K doesn't depend on how bright
    the reference monitor was, so K from different people can be compared and
    averaged. A later session on the same phone and kind of screen uses
    K . a_cam instead of a_cam. Returns None if either matrix is unusable.
    """
    try:
        y_ref = float((a_ref @ white_cam)[1])
        y_cam = float((a_cam @ white_cam)[1])
        if not (y_ref > 0 and y_cam > 0):
            return None
        k = (a_ref * (y_cam / y_ref)) @ np.linalg.inv(a_cam)
    except np.linalg.LinAlgError:
        return None
    return k if valid_correction(k) else None


def valid_correction(k):
    """Is k a sane 3x3 correction? Used on anything that comes from outside, such as the shared database."""
    try:
        k = np.asarray(k, dtype=float)
        return bool(k.shape == (3, 3) and np.isfinite(k).all() and np.linalg.det(k) > 0 and np.linalg.cond(k) < MAX_CORRECTION_COND)
    except (TypeError, ValueError):
        return False


def white_after_correction(k):
    """CCT and Duv of a D65 screen white as seen through correction k (a measure of how big k is)."""
    return cm.cct_duv(np.asarray(k, dtype=float) @ cm.D65)


def _grays(photo, a, corr, curve):
    """XYZ of every patch on a bright chart plus the gray-scale steps."""
    patches = photo["result"]["patches"]
    xyz = {key: a @ _sig(patches[i], corr) for i, (key, _) in enumerate(chart.BRIGHT_PATCHES)}
    white = xyz["W"]
    yw = white[1]
    if yw <= 0:
        return None
    ref_white = cm.D65 * yw
    steps = []
    for key, rgb in chart.BRIGHT_PATCHES:
        if not (key.startswith("g") or key == "W"):
            continue
        v = rgb[0] / 255
        x = xyz[key]
        y = float(x[1] / yw)
        yt = curve(v)
        target_lab = cm.xyz_to_lab(ref_white * yt, ref_white)
        step = {
            "key": key,
            "level": round(v * 100),
            "code": rgb[0],
            "Y": y,
            "Yt": yt,
            "gamma": math.log(y) / math.log(v) if 0 < v < 1 and y > 0 else None,
            "balance": cm.rgb_balance(x),
            "xy": cm.xy(x),
            "dE": cm.delta_e2000(cm.xyz_to_lab(x, ref_white), target_lab),
            "dEtint": cm.delta_e2000(cm.xyz_to_lab(x * (yt / y), ref_white), target_lab) if y > 0 else None,
        }
        steps.append(step)
    steps.sort(key=lambda s: s["level"])
    return xyz, steps


def _white_offset(white_xyz, de):
    """Where white sits relative to D65, in ΔE: along warm/cool and green/magenta."""
    u, v = cm.uv1960(white_xyz)
    u0, v0 = cm.uv1960(cm.D65)
    warmer = np.array(cm.planck_uv(6000)) - np.array(cm.planck_uv(7000))  # locus direction toward warmer
    warmer /= np.linalg.norm(warmer)
    greener = np.array([-warmer[1], warmer[0]])
    if greener[1] < 0:  # above the locus (higher v) is greenish
        greener = -greener
    d = np.array([u - u0, v - v0])
    if np.linalg.norm(d) == 0:
        return {"warm": 0.0, "green": 0.0}
    angle = math.atan2(d @ greener, d @ warmer)
    return {"warm": float(de * math.cos(angle)), "green": float(de * math.sin(angle))}


def _gains(xyz, white):
    """How R, G and B add up to white, and the gain changes that would make it D65.

    The basis is each channel's XYZ contribution to the measured white (white
    Y = 1), so white at new gains is basis @ (new / current). It comes from the
    monitor's own red, green and blue patches, rescaled so they add up exactly
    to the measured white. If they don't add up even roughly (dynamic
    contrast, clipping), sRGB primaries are assumed instead.
    """
    m = np.column_stack([xyz["R"], xyz["G"], xyz["B"]])
    additivity = float(np.linalg.norm(white - m.sum(axis=1)) / np.linalg.norm(white))
    basis, k, method = None, None, "srgb"
    if additivity < 0.08:
        try:
            s = np.linalg.solve(m, white)
            if (s > 0).all():
                basis = m * s
                k = np.linalg.solve(basis, cm.D65 * white[1])
                method = "measured"
        except np.linalg.LinAlgError:
            basis = None
    if basis is None or k is None or not (k > 0).all():
        lin = np.clip(cm.XYZ_TO_SRGB @ white, 1e-6, None)
        basis = cm.SRGB_TO_XYZ * lin
        k = np.linalg.solve(basis, cm.D65 * white[1])
        method = "srgb"
    k = k / k.max()
    return {
        "k": [float(x) for x in k],
        "y": [float(x) for x in basis[1] / white[1]],
        "basis": [[float(v) for v in col] for col in (basis / white[1]).T],
        "method": method,
    }


def _fit_gamma(steps):
    pts = [(math.log(s["level"] / 100), math.log(s["Y"])) for s in steps if 20 <= s["level"] <= 90 and s["Y"] > 0]
    if not pts:
        return None
    return sum(lx * ly for lx, ly in pts) / sum(lx * lx for lx, _ in pts)


def _round(photo, a, corr, curve, ref_white_y):
    res = _grays(photo, a, corr, curve)
    if res is None:
        return None
    xyz, steps = res
    w = xyz["W"]
    cct, duv = cm.cct_duv(w)
    white_lab = cm.xyz_to_lab(w, cm.D65 * w[1])
    de = cm.delta_e2000(white_lab, [100.0, 0.0, 0.0])
    prim = {k: cm.xy(xyz[k]) for k in ("R", "G", "B")}
    gamut = cm.triangle_area([prim["R"], prim["G"], prim["B"]]) / cm.triangle_area(cm.SRGB_PRIMARIES_XY)
    return {
        "photo": photo["id"],
        "name": photo["name"],
        "white": {
            "xy": cm.xy(w),
            "cct": cct,
            "duv": duv,
            "dE": de,
            "balance": cm.rgb_balance(w),
            "Y": float(w[1]),
            "offset": _white_offset(w, de),
        },
        "gains": _gains(xyz, w),
        "brightness": float(w[1] / ref_white_y) if ref_white_y else None,
        "gamma": _fit_gamma(steps),
        "steps": steps,
        "maxTint": max((s["dEtint"] or 0) for s in steps),
        "avgDE": float(np.mean([s["dE"] for s in steps])),
        "primaries": prim,
        "gamut": gamut,
        "whiteConsistency": float(xyz["W2"][1] / w[1] - 1),
    }


def _level_y(steps, v):
    """Relative luminance at code value v (0-1), interpolated log-log from gray steps."""
    pts = sorted((s["code"] / 255, s["Y"]) for s in steps if 0 < s["Y"] and s["code"] < 255)
    if len(pts) < 2:
        return None
    i = 0
    while i < len(pts) - 2 and pts[i + 1][0] < v:
        i += 1
    (v0, y0), (v1, y1) = pts[i], pts[i + 1]
    g = math.log(y1 / y0) / math.log(v1 / v0)
    return y0 * (v / v0) ** g


def _near_black(photo, a, corr, curve, white_xyz, white_cam, round_steps):
    """Dark steps relative to white.

    Preferred: scale by the chart's own corner squares (a known gray, same
    photo, same exposure), using the bright chart only for the shape of the
    tone curve. That stays right if brightness changed between photos.
    Fallback: compare with the white measured in the bright chart photo.
    """
    k_y = 1 / white_xyz[1]
    k_cam = 1 / max(float(np.mean(white_cam)), 1e-12)
    self_ref = False
    marks = photo["result"].get("marks") or []
    mark_y = _level_y(round_steps, chart.MARK_LEVEL["dark"] / 255)
    if len(marks) >= 3 and mark_y:
        cams = [_sig(m, corr) for m in marks]
        y_mark = float(np.median([(a @ c)[1] for c in cams]))
        cam_mark = float(np.median([np.mean(c) for c in cams]))
        if y_mark > 0 and cam_mark > 0:
            k_y, k_cam, self_ref = mark_y / y_mark, mark_y / cam_mark, True

    steps = []
    for (key, rgb), p in zip(chart.DARK_PATCHES, photo["result"]["patches"]):
        v = rgb[0] / 255
        y = float((a @ _sig(p, corr))[1] * k_y)
        yt = curve(v)
        noise = p["noise"] * k_cam
        if yt < 6 * noise:
            status, ratio = "faint", None
        else:
            ratio = y / yt
            if ratio < 0.3:
                status = "crushed"
            elif ratio < 0.6:
                status = "dark"
            elif ratio > 1.6:
                status = "raised"
            else:
                status = "ok"
        steps.append({"key": key, "level": round(v * 100, 1), "code": rgb[0], "Y": y, "Yt": yt,
                      "ratio": ratio, "noise": noise, "status": status})
    return steps, self_ref


def _tint_name(dev):
    """Name the color cast from per-channel deviations (percent)."""
    i_hi, i_lo = int(np.argmax(dev)), int(np.argmin(dev))
    if dev[i_hi] >= -dev[i_lo]:
        return COLOR_NAMES[i_hi][0]
    return COLOR_NAMES[i_lo][1]


def _advice(latest, near, ref_check, method, target_gamma, names):
    tgt = names.get("target") or "the monitor"
    out = []

    bal = np.array(latest["white"]["balance"])
    dev = bal / bal.mean() * 100 - 100
    if method == "camera":
        # The phone's built-in matrix is fitted to everyday scenes, not to the
        # narrow red/green/blue peaks of a screen, so don't steer gains by it.
        out.append({
            "level": "warn",
            "title": f"White reads {latest['white']['cct']:.0f} K, but don't adjust RGB gains by this yet",
            "detail": ("Without shot 1 on a reference monitor, white is measured with the phone's built-in color data, which "
                       "can be off by several hundred kelvin on OLED screens. Take shot 1 first, then adjust."),
        })
    elif latest["white"]["dE"] <= 1.5 and np.abs(dev).max() <= 1.5:
        out.append({"level": "ok", "title": "White point is on target",
                    "detail": f"White measures {latest['white']['cct']:.0f} K, within {latest['white']['dE']:.1f} ΔE of D65."})
    else:
        warm = "too cool (bluish)" if latest["white"]["cct"] > 6504 else "too warm (yellowish)"
        out.append({
            "level": "adjust",
            "title": f"White is {warm} with a {_tint_name(dev)} cast",
            "detail": (f"White measures {latest['white']['cct']:.0f} K, {latest['white']['dE']:.1f} ΔE from D65 (under 2 counts as "
                       f"accurate). In {tgt}'s menu, set color temperature to User/Custom and set Red, Green and Blue to the "
                       "values in the White point panel below. Check the picture on screen actually changes, then retake shot 3."),
            "note": "If the picture mode locks the RGB controls, try its color temperature presets instead, or accept the small error.",
        })

    g = latest["gamma"]
    if g is not None:
        if abs(g - target_gamma) <= 0.08:
            out.append({"level": "ok", "title": "Gamma is on target", "detail": f"Midtones measure gamma {g:.2f} (target {target_gamma:.1f})."})
        else:
            darker = g > target_gamma
            out.append({
                "level": "adjust",
                "title": "Midtones are too dark" if darker else "Midtones are too bright",
                "detail": (f"Measured gamma is {g:.2f}, target {target_gamma:.1f}. If {tgt}'s menu has a gamma setting, "
                           f"pick a {'lower' if darker else 'higher'} one. Then retake shot 3."),
            })

    # Gray tint is judged against this monitor's own white, so a white point
    # error (or a matrix bias) doesn't make every gray look tinted.
    whites = np.array(latest["white"]["balance"])
    casts = []
    for s in latest["steps"]:
        if s["level"] >= 100:
            continue
        d = np.array(s["balance"]) / np.array(s["balance"]).mean() * 100 - (whites / whites.mean() * 100)
        if np.abs(d).max() > 4:
            casts.append((s["level"], _tint_name(d)))
    if casts:
        lv = ", ".join(f"{lvl}%" for lvl, _ in casts)
        tint = max(set(t for _, t in casts), key=[t for _, t in casts].count)
        out.append({
            "level": "warn",
            "title": f"Some grays have a {tint} tint",
            "detail": (f"The {lv} gray steps differ in color from white. Fix the white point first. Only monitors with "
                       "a multi-point gray-scale adjustment can correct the rest."),
        })

    if latest["brightness"] is not None and names.get("reference"):
        b = latest["brightness"]
        if b > 1.1 or b < 0.9:
            out.append({
                "level": "adjust",
                "title": f"{tgt} is {'brighter' if b > 1 else 'dimmer'} than {names['reference']}",
                "detail": (f"Its white is {b:.2f}× the reference's. To compare the two side by side, "
                           f"{'lower' if b > 1 else 'raise'} its Brightness until they match. This doesn't affect color accuracy."),
            })

    if latest["gamut"] > 1.15:
        out.append({
            "level": "adjust",
            "title": "Colors are wider than sRGB",
            "detail": (f"Red, green and blue cover about {latest['gamut'] * 100:.0f}% of the sRGB area, so normal content looks "
                       "oversaturated. Switch the monitor to its sRGB mode."),
            "note": "This estimate is rougher than the white and gray readings.",
        })

    if near:
        crushed = [s["level"] for s in near if s["status"] == "crushed"]
        raised = [s["level"] for s in near if s["status"] == "raised"]
        dark = [s["level"] for s in near if s["status"] == "dark"]
        if crushed:
            out.append({
                "level": "adjust",
                "title": "The darkest shades are lost",
                "detail": (f"Steps up to {max(crushed):g}% show almost no light. Check that the graphics driver outputs "
                           "Full range RGB, then try raising Black level or Shadow boost if the monitor has it."),
            })
        elif raised:
            out.append({
                "level": "adjust",
                "title": "Near-black is lifted",
                "detail": (f"Steps around {', '.join(f'{x:g}%' for x in raised[:3])} are brighter than they should be, which makes "
                           "blacks look gray. Check for a Full/Limited range mismatch, or lower Black level."),
            })
        elif dark:
            out.append({
                "level": "warn",
                "title": "Shadows are a bit dark",
                "detail": f"Steps {', '.join(f'{x:g}%' for x in dark[:4])} are dimmer than target, but still visible.",
            })
        elif any(s["status"] == "ok" for s in near):
            out.append({"level": "ok", "title": "Near-black detail is fine", "detail": "Every measurable dark step is visible and close to target."})

    if ref_check and ref_check["maxTint"] > 4:
        out.append({
            "level": "warn",
            "title": "The reference monitor may be off",
            "detail": (f"Its grays vary by up to {ref_check['maxTint']:.1f} ΔE. Check that it's in its factory-calibrated mode, "
                       "because everything else is measured against it."),
        })
    if method == "camera":
        out.append({
            "level": "warn",
            "title": "Measured without a reference monitor",
            "detail": "White point and colors are rough. Gamma, gray tint relative to white, and near-black readings are still reliable.",
        })
    elif method == "profile":
        out.append({
            "level": "ok",
            "title": "Measured with a saved camera correction",
            "detail": ("No reference shot, so the phone's color data was corrected with a profile learned on the same phone and kind "
                       "of screen. White point is much better than the phone's own data, but not as good as a reference shot."),
        })
    return out


def _usable_correction(correction):
    if not correction:
        return None
    k = np.asarray(correction.get("k"), dtype=float) if correction.get("k") is not None else None
    return k if k is not None and valid_correction(k) else None


def _correction_info(ref, corr, a_ref, additivity, reference, flat_resid):
    """What a reference shot teaches about this phone, and whether it's good enough to save or share."""
    a_cam = _camera_matrix(ref)
    if a_cam is None:
        return None
    white_cam = _sig(ref["result"]["patches"][BRIGHT_INDEX["W"]], corr)
    k = correction_matrix(a_ref, a_cam, white_cam)
    if k is None:
        return None
    scale = float((a_cam @ white_cam)[1] / (a_ref @ white_cam)[1])
    problems = []
    if additivity >= 0.06:
        problems.append("The reference's white doesn't equal red + green + blue.")
    if reference:
        if not 1.9 <= (reference["gamma"] or 0) <= 2.6:
            problems.append("The reference's gamma is unusual.")
        if reference["maxTint"] >= 4:
            problems.append("The reference's grays are tinted.")
    if flat_resid is None:
        problems.append("There is no white chart (shot 2) to correct lens falloff.")
    elif flat_resid > 0.05:
        problems.append("The white chart is uneven.")
    if ref["result"].get("peak", 0) > 0.95 or any(p.get("clipped") for p in ref["result"]["patches"]):
        problems.append("The reference photo is overexposed.")
    return {
        "k": k.tolist(),
        "aRef": (a_ref * scale).tolist(),
        "aCam": a_cam.tolist(),
        "whiteCam": white_cam.tolist(),
        "good": not problems,
        "problems": problems,
    }


def _uv(xy):
    x, y = xy
    d = -2 * x + 12 * y + 3
    return np.array([4 * x / d, 6 * y / d])


def analyze_session(photos, target="2.2", names=None, ref_spec=None, correction=None):
    """Measure a session. `correction` is a saved or shared phone correction (see correction_matrix),
    used only when the session has no reference shot: {"k": 3x3, "label", "source", "contributors", "spreadK"}."""
    names = names or {}
    curve = cm.target_curve(target)
    target_gamma = cm.target_gamma(target)
    spec = reference_spec(ref_spec)
    out = {"target": target, "warnings": [], "advice": [], "rounds": [], "nearBlack": None,
           "reference": None, "method": None, "flatField": None, "crossCheck": None,
           "referenceSpec": {"name": spec["name"], "white": spec["white"]},
           "correction": None, "profile": None}
    if spec.get("invalid"):
        out["warnings"].append("The custom reference values aren't valid, so the reference is treated as sRGB. Check them in Setup.")

    usable = [p for p in photos if p.get("result") and identity(p)]
    stamps = [p["result"].get("timestamp") or 0 for p in usable]
    if all(stamps):
        usable.sort(key=lambda p: (p["result"]["timestamp"], p["id"]))
    else:
        usable.sort(key=lambda p: p["id"])

    def latest(t, r):
        found = [p for p in usable if identity(p) == (t, r)]
        return found[-1] if found else None

    ref = latest("bright", "reference")
    flat = latest("white", "target")
    dark = latest("dark", "target")
    rounds = [p for p in usable if identity(p) == ("bright", "target")]
    out["have"] = {"reference": bool(ref), "white": bool(flat), "dark": bool(dark), "rounds": len(rounds)}

    if flat:
        corr, resid = _flat_field(flat)
        if corr is None:
            corr = _no_corr
            out["warnings"].append("Shot 2 couldn't be used to correct lens falloff. Retake it.")
        else:
            out["flatField"] = {"residual": resid}
            if resid > 0.05:
                out["warnings"].append("Shot 2 looks uneven (more than lens falloff explains). Check for reflections and retake it.")
    else:
        corr = _no_corr
        if rounds:
            out["warnings"].append("Shot 2 (white chart) is missing, so lens falloff isn't corrected. Results are less accurate.")

    a = None
    ref_white_y = None
    flat_resid = (out["flatField"] or {}).get("residual")
    if ref:
        a_ref, additivity = _reference_matrix(ref, corr, spec)
        if np.isfinite(a_ref).all():
            a = a_ref
            out["method"] = "reference"
            res = _grays(ref, a, corr, curve)
            if res:
                xyz, steps = res
                ref_white_y = float(xyz["W"][1])
                out["reference"] = {
                    "additivity": additivity,
                    "gamma": _fit_gamma(steps),
                    "maxTint": max((s["dEtint"] or 0) for s in steps),
                    "steps": steps,
                }
            if additivity > 0.06:
                out["warnings"].append(
                    "On the reference monitor, white doesn't equal red + green + blue. Its dynamic contrast or a similar setting may be on.")
            out["correction"] = _correction_info(ref, corr, a_ref, additivity, out["reference"], flat_resid)
    a_cam = next((m for m in (_camera_matrix(p) for p in rounds + ([ref] if ref else [])) if m is not None), None)
    if a is None and a_cam is not None:
        k = _usable_correction(correction)
        if k is not None:
            a = k @ a_cam
            out["method"] = "profile"
            out["profile"] = {key: correction.get(key) for key in ("label", "source", "contributors", "spreadK")}
        else:
            a = a_cam
            out["method"] = "camera"

    if not rounds:
        out["warnings"].append("Add shot 3 (bright chart on the monitor you're calibrating) to see results.")
        return out
    if a is None:
        out["warnings"].append("Shot 1 (reference) is missing and the photos have no camera color data, so colors can't be measured.")
        return out

    summaries = []
    for p in rounds:
        r = _round(p, a, corr, curve, ref_white_y)
        if r:
            summaries.append(r)
    if not summaries:
        out["warnings"].append("Shot 3 came out too dark to measure. Retake it.")
        return out
    for i, r in enumerate(summaries, start=1):
        r["round"] = i
    out["rounds"] = summaries
    latest_round = summaries[-1]

    if len(summaries) >= 2:
        prev = summaries[-2]
        shift = float(np.linalg.norm(_uv(latest_round["white"]["xy"]) - _uv(prev["white"]["xy"])))
        if shift < 0.0012:
            change = latest_round["white"]["Y"] / prev["white"]["Y"] - 1
            note = f", only its brightness ({change * 100:+.0f}%)" if abs(change) > 0.1 else ""
            out["warnings"].append(
                f"White hasn't changed color since round {prev['round']}{note}. If you changed the RGB gains, the current "
                "picture mode probably locks them (many sRGB modes do). Check they're adjustable in the mode you're measuring.")

    if abs(latest_round["whiteConsistency"]) > 0.05:
        out["warnings"].append(
            "The two white patches on shot 3 disagree. The phone may have moved since shot 2. Retake shot 2, then shot 3.")

    if out["method"] == "reference" and a_cam is not None:
        w = a_cam @ _sig(next(p for p in rounds if p["id"] == latest_round["photo"])["result"]["patches"][chart.CENTER_INDEX], corr)
        if w[1] > 0:
            cct, duv = cm.cct_duv(w)
            out["crossCheck"] = {"cct": cct, "duv": duv}

    if dark:
        # Pair the dark chart with the bright chart taken closest in time, when
        # the monitor was most likely in the same state.
        pairs = [(r, p) for r, p in zip(summaries, rounds) if r["photo"] == p["id"]]
        stamp = dark["result"].get("timestamp") or 0
        if stamp and all(p["result"].get("timestamp") for _, p in pairs):
            base, base_photo = min(pairs, key=lambda rp: abs(rp[1]["result"]["timestamp"] - stamp))
        else:
            before = [rp for rp in pairs if rp[1]["id"] <= dark["id"]]
            base, base_photo = before[-1] if before else pairs[0]
        white_cam = _sig(base_photo["result"]["patches"][chart.CENTER_INDEX], corr)
        white_xyz = a @ white_cam
        steps, self_ref = _near_black(dark, a, corr, curve, white_xyz, white_cam, base["steps"])
        out["nearBlack"] = {"round": base["round"], "steps": steps, "selfReferenced": self_ref}

    out["advice"] = _advice(latest_round, out["nearBlack"]["steps"] if out["nearBlack"] else None,
                            out["reference"], out["method"], target_gamma, names)
    return out
