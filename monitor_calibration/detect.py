"""Find the chart in a photo, read its ID code and measure every patch."""

import numpy as np
from scipy import ndimage

from . import chart

PATCH_SHRINK = 0.15  # measure only the middle of each patch, away from blurred edges
CLIP_LEVEL = 0.985
NEAR_CLIP = 0.8


class ChartNotFound(Exception):
    pass


def homography(src, dst):
    """3x3 projective map taking src points to dst points (4+ pairs)."""
    rows = []
    for (x, y), (u, v) in zip(src, dst):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    _, _, vt = np.linalg.svd(np.asarray(rows, dtype=float))
    h = vt[-1].reshape(3, 3)
    return h / h[2, 2]


def apply_h(h, pts):
    pts = np.asarray(pts, dtype=float).reshape(-1, 2)
    p = np.c_[pts, np.ones(len(pts))] @ h.T
    return p[:, :2] / p[:, 2:3]


def _downsample(lum, max_w=1600):
    f = max(1, int(np.ceil(lum.shape[1] / max_w)))
    h, w = (lum.shape[0] // f) * f, (lum.shape[1] // f) * f
    return lum[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3)), f


def _blobs(lum_small):
    hi = np.percentile(lum_small, 99.7)
    if hi <= 1e-4:
        raise ChartNotFound("The photo is almost completely dark. Check that the chart was on screen.")
    labels, n = ndimage.label(lum_small > 0.3 * hi)
    if n == 0:
        raise ChartNotFound("Couldn't find anything bright in the photo.")
    flat = labels.ravel()
    area = np.bincount(flat, minlength=n + 1).astype(float)
    ys, xs = np.indices(lum_small.shape)
    sx = np.bincount(flat, weights=xs.ravel(), minlength=n + 1)
    sy = np.bincount(flat, weights=ys.ravel(), minlength=n + 1)
    min_area = 1.5e-5 * lum_small.size
    blobs = []
    for i, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None or area[i] < min_area:
            continue
        bh, bw = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        blobs.append({
            "area": area[i],
            "cx": sx[i] / area[i],
            "cy": sy[i] / area[i],
            "fill": area[i] / (bh * bw),
            "aspect": bw / bh,
        })
    return blobs


def _corner_blobs(blobs, shape):
    """Pick the four corner squares: the blobs furthest out along each diagonal."""
    h, w = shape
    cand = [
        b for b in blobs
        if 0.1 * w < b["cx"] < 0.9 * w and 0.1 * h < b["cy"] < 0.9 * h
        and b["fill"] > 0.45 and 0.4 < b["aspect"] < 2.5
    ]
    if len(cand) < 4:
        raise ChartNotFound(
            "Couldn't find the chart's four corner squares. Keep the chart in the middle of the frame and retake the photo."
        )
    xs = np.array([b["cx"] for b in cand])
    ys = np.array([b["cy"] for b in cand])
    picks = [int(np.argmin(xs + ys)), int(np.argmax(xs - ys)), int(np.argmax(xs + ys)), int(np.argmin(xs - ys))]
    corners = [cand[i] for i in picks]
    areas = [c["area"] for c in corners]
    if len(set(picks)) < 4 or max(areas) > 3 * min(areas):
        raise ChartNotFound(
            "Couldn't find the chart's four corner squares. Make sure the whole chart is in the photo and no lights are in the frame."
        )
    return corners


def _region(img, h, h_inv, rect, shrink=0.0):
    """Pixels of img inside a chart-space rectangle (shrunk on each side)."""
    x, y, w, hh = rect
    x0, x1 = x + w * shrink, x + w * (1 - shrink)
    y0, y1 = y + hh * shrink, y + hh * (1 - shrink)
    corners = apply_h(h, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    ix0 = max(int(np.floor(corners[:, 0].min())), 0)
    ix1 = min(int(np.ceil(corners[:, 0].max())) + 1, img.shape[1])
    iy0 = max(int(np.floor(corners[:, 1].min())), 0)
    iy1 = min(int(np.ceil(corners[:, 1].max())) + 1, img.shape[0])
    if ix1 <= ix0 or iy1 <= iy0:
        return None
    yy, xx = np.mgrid[iy0:iy1, ix0:ix1]
    c = apply_h(h_inv, np.c_[xx.ravel(), yy.ravel()])
    inside = (c[:, 0] >= x0) & (c[:, 0] <= x1) & (c[:, 1] >= y0) & (c[:, 1] <= y1)
    if inside.sum() < 9:
        return None
    block = img[iy0:iy1, ix0:ix1]
    return block.reshape(-1, block.shape[-1] if block.ndim == 3 else 1)[inside]


def _trimmed(pixels, trim=0.05):
    """Mean per channel after dropping the brightest/darkest few percent (dust, hot pixels)."""
    lum = pixels.mean(axis=1)
    lo, hi = np.quantile(lum, [trim, 1 - trim])
    keep = (lum >= lo) & (lum <= hi)
    sel = pixels[keep] if keep.sum() >= 5 else pixels
    return sel.mean(axis=0), float(sel.mean(axis=1).std() / np.sqrt(len(sel))), int(len(sel))


def _decode(lum, h, h_inv):
    fid = []
    for fx, fy in chart.FIDUCIALS:
        px = _region(lum, h, h_inv, (fx, fy, chart.FIDUCIAL_SIZE, chart.FIDUCIAL_SIZE), 0.25)
        if px is None:
            return None
        fid.append(float(np.median(px)))
    ref = float(np.median(fid))
    if ref <= 0:
        return None
    bits = []
    for i in range(len(chart.BITS_X)):
        px = _region(lum, h, h_inv, chart.bit_rect(i), 0.25)
        if px is None:
            return None
        bits.append(1 if float(np.median(px)) > 0.5 * ref else 0)
    return chart.decode_bits(bits)


def _gap_strips(rect):
    """Strips down the middle of the black gaps on all four sides of a patch."""
    x, y, w, h = rect
    g = chart.GAP
    ys = (y + h * PATCH_SHRINK, h * (1 - 2 * PATCH_SHRINK))
    xs = (x + w * PATCH_SHRINK, w * (1 - 2 * PATCH_SHRINK))
    return [
        (x - 0.7 * g, ys[0], 0.4 * g, ys[1]),
        (x + w + 0.3 * g, ys[0], 0.4 * g, ys[1]),
        (xs[0], y - 0.7 * g, xs[1], 0.4 * g),
        (xs[0], y + h + 0.3 * g, xs[1], 0.4 * g),
    ]


def measure(img, meta):
    """Measure one photo. Raises ChartNotFound if the chart can't be located."""
    warnings = []
    lum = img.mean(axis=2)
    small, f = _downsample(lum)
    corners = _corner_blobs(_blobs(small), small.shape)
    img_pts = [(c["cx"] * f + (f - 1) / 2, c["cy"] * f + (f - 1) / 2) for c in corners]  # TL, TR, BR, BL

    found = None
    for k in range(4):  # the phone may have been held at any rotation
        dst = [img_pts[(i + k) % 4] for i in range(4)]
        h = homography(chart.FIDUCIAL_CENTERS, dst)
        h_inv = np.linalg.inv(h)
        ident = _decode(lum, h, h_inv)
        if ident:
            found = (k, h, h_inv, ident)
            break
    if found is None:
        h = homography(chart.FIDUCIAL_CENTERS, img_pts)
        found = (0, h, np.linalg.inv(h), None)
        warnings.append("Couldn't read the chart's ID code. Pick which shot this is in the list.")
    rotation, h, h_inv, ident = found

    shutter, iso = meta.get("shutter") or 0.0, meta.get("iso") or 0.0
    if shutter > 0 and iso > 0:
        norm = shutter * iso / 100.0
    else:
        norm = 1.0
        warnings.append("The photo has no shutter speed or ISO info. Keep the same camera settings for every photo.")

    rows, cols = img.shape[:2]
    patches = []
    clipped = 0
    for i in range(chart.COLS * chart.ROWS):
        rect = chart.patch_rect(i)
        px = _region(img, h, h_inv, rect, PATCH_SHRINK)
        if px is None:
            raise ChartNotFound("Part of the chart is outside the photo. Keep the whole chart in the frame.")
        mean, sem, n = _trimmed(px)
        gap = [g for g in (_region(img, h, h_inv, r) for r in _gap_strips(rect)) if g is not None]
        if gap:
            # A trimmed mean, not a median: near-black sensor values are almost
            # whole numbers, and a median snaps to them.
            flare, flare_sem, _ = _trimmed(np.concatenate(gap), 0.1)
        else:
            flare, flare_sem = np.zeros(3), 0.0
        clip_frac = float((px.max(axis=1) >= CLIP_LEVEL).mean())
        if clip_frac > 0.005:
            clipped += 1
        center = apply_h(h, [(rect[0] + rect[2] / 2, rect[1] + rect[3] / 2)])[0]
        patches.append({
            "index": i,
            "pos": [float(center[0] / cols), float(center[1] / rows)],
            "signal": ((mean - flare) / norm).tolist(),
            "raw": mean.tolist(),
            "flare": (flare / norm).tolist(),
            "noise": float(np.hypot(sem, flare_sem) / norm),
            "n": n,
            "clipped": clip_frac > 0.005,
        })

    # Corner squares: a known gray level in the same photo. The dark chart is
    # scaled against them, so it doesn't depend on another photo's brightness.
    marks = []
    for (fx, fy), near in zip(chart.FIDUCIALS, (0, 4, 14, 10)):
        px = _region(img, h, h_inv, (fx, fy, chart.FIDUCIAL_SIZE, chart.FIDUCIAL_SIZE), 0.25)
        if px is None:
            continue
        mean, _, _ = _trimmed(px)
        center = apply_h(h, [(fx + chart.FIDUCIAL_SIZE / 2, fy + chart.FIDUCIAL_SIZE / 2)])[0]
        marks.append({
            "pos": [float(center[0] / cols), float(center[1] / rows)],
            "signal": (mean / norm - np.array(patches[near]["flare"])).tolist(),
        })

    # Framing checks. Distances use the fiducial centers in image space.
    tl, tr, br, bl = apply_h(h, chart.FIDUCIAL_CENTERS)
    top, bottom = np.linalg.norm(tr - tl), np.linalg.norm(br - bl)
    left, right = np.linalg.norm(bl - tl), np.linalg.norm(br - tr)
    sideways = rotation in (1, 3)
    frame_w = rows if sideways else cols
    chart_frac = (top + bottom) / 2 / (95.0 / 100.0) / frame_w
    keystone = max(top / bottom, bottom / top, left / right, right / left)
    angle = float(np.degrees(np.arctan2(*(tr - tl)[::-1])))
    center = apply_h(h, [(chart.WIDTH / 2, chart.HEIGHT / 2)])[0]
    offset = float(np.hypot(center[0] / cols - 0.5, center[1] / rows - 0.5))

    if sideways:
        warnings.append("The chart is sideways in the photo. Hold the phone in landscape, even for a monitor that stands in "
                        "portrait, and check the chart looks upright on that screen.")
    if chart_frac < 0.26:
        warnings.append("The chart is small in the frame. Move the phone closer until the corner squares touch the middle grid square.")
    elif chart_frac > 0.42:
        warnings.append("The chart is large in the frame. Move the phone back until the corner squares sit on the middle grid square.")
    if keystone > 1.06:
        warnings.append("The chart looks like a trapezoid, so the phone isn't pointing straight at the screen.")
    if offset > 0.08:
        warnings.append("The chart isn't centered in the frame.")
    peak = max(max(p["raw"]) for p in patches)
    if clipped:
        warnings.append(f"{clipped} patch(es) are overexposed. Use a faster shutter speed (for example 1/120 s) and retake.")
    elif peak > NEAR_CLIP:
        # Phone sensors stop being linear near the top of their range.
        warnings.append(f"The brightest patches use {peak * 100:.0f}% of the sensor's range, close to overexposed, which can skew "
                        "the readings. Use a faster shutter speed (for example 1/120 s) and retake.")
    glare = float(np.median([np.mean(p["flare"]) * norm for p in patches]))
    if peak > 0 and glare / peak > 0.02:
        warnings.append("There's a lot of stray light in the photo. Make the room darker and check for reflections.")

    return {
        "chart": {"type": ident[0], "role": ident[1]} if ident else None,
        "rotation": rotation,
        "size": [cols, rows],
        "chartFraction": float(chart_frac),
        "keystone": float(keystone),
        "angle": angle,
        "offset": offset,
        "exposure": {"shutter": shutter, "iso": iso, "norm": norm},
        "timestamp": meta.get("timestamp") or 0.0,
        "camToXyz": meta.get("cam_to_xyz"),
        "capture": meta.get("capture") or {},
        "peak": float(peak),
        "patches": patches,
        "marks": marks,
        "warnings": warnings,
    }
