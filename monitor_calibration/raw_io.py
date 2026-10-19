"""Load a phone RAW (DNG) file as linear camera RGB."""

import numpy as np
import rawpy

from . import privacy, tiff_meta


class NotRawError(Exception):
    pass


def load_raw(path):
    """Return (image, meta).

    image is float32 H x W x 3, linear camera-native RGB with the black level
    removed and 1.0 = sensor saturation. Bayer data is reduced by merging each
    2x2 block (no demosaic interpolation) and kept in sensor orientation, so
    lens falloff lines up between photos. No white balance or color matrix is
    applied.

    The black level is subtracted here in floating point rather than by
    LibRaw, which clips negative noise to zero and would bias near-black
    readings upward.
    """
    kind = _sniff(path)
    if kind in ("JPEG", "HEIC", "PNG"):
        raise NotRawError(
            f"This is a {kind} image, not a RAW photo, even if its name ends in .DNG. The RAW data was removed when "
            "the photo was sent or shared (messaging apps and the iPhone photo picker do this). Export the original "
            "file, or copy it from the phone with a USB cable."
        )
    try:
        raw = rawpy.imread(str(path))
    except (rawpy.LibRawError, OSError) as e:
        if kind == "TIFF":
            raise NotRawError(
                "This DNG couldn't be decoded. It may use a compression the analyzer doesn't support yet, such as JPEG XL."
            ) from e
        raise NotRawError(
            "This file isn't a RAW photo the analyzer can read. Make sure the camera app saves RAW (DNG)."
        ) from e

    with raw:
        exposure = _exposure(path, raw)
        cam_to_xyz = _camera_to_xyz(raw)
        black = np.array(raw.black_level_per_channel, dtype=np.float32)
        white = float(raw.white_level)
        data = raw.raw_image_visible

        if raw.raw_type == rawpy.RawType.Flat:
            colors = raw.raw_colors_visible
            desc = raw.color_desc.decode("ascii")
            h, w = (data.shape[0] // 2) * 2, (data.shape[1] // 2) * 2
            image = np.zeros((h // 2, w // 2, 3), dtype=np.float32)
            counts = [0, 0, 0]
            for dy in (0, 1):
                for dx in (0, 1):
                    ci = int(colors[dy, dx])
                    ch = "RGB".index(desc[ci])
                    plane = data[dy:h:2, dx:w:2].astype(np.float32)
                    plane -= black[ci]
                    plane /= white - black[ci]
                    image[..., ch] += plane
                    counts[ch] += 1
            if 0 in counts:
                raise NotRawError("This camera's sensor layout isn't supported.")
            image /= np.array(counts, dtype=np.float32)
        else:
            if data.ndim != 3 or data.shape[2] < 3:
                raise NotRawError("This RAW format isn't supported.")
            image = data[..., :3].astype(np.float32)
            image -= black[:3]
            image /= white - black[:3]

    meta = dict(exposure, cam_to_xyz=cam_to_xyz.tolist() if cam_to_xyz is not None else None, capture=_capture(path))
    return image, meta


def _capture(path):
    """The camera fields that may be shared (allow-list only), or {} if the file's tags can't be read."""
    try:
        return privacy.clean_capture(tiff_meta.read_capture(path))
    except Exception:  # damaged tags shouldn't stop the analysis
        return {}


def _exposure(path, raw):
    """Shutter, ISO and capture time: from the file's own tags, else from rawpy if it has them."""
    try:
        tags = tiff_meta.read_exposure(path)
    except Exception:  # damaged tags shouldn't stop the analysis
        tags = None
    if tags and tags["shutter"] > 0 and tags["iso"] > 0:
        return tags
    other = getattr(raw, "other", None)  # added in newer rawpy versions
    if other is None:
        return tags or {"shutter": 0.0, "iso": 0.0, "timestamp": 0.0}
    stamp = other.timestamp
    if hasattr(stamp, "timestamp"):  # rawpy returns a datetime
        try:
            stamp = stamp.timestamp()
        except (OverflowError, OSError, ValueError):
            stamp = 0.0
    return {
        "shutter": float(other.shutter_speed or 0.0),
        "iso": float(other.iso_speed or 0.0),
        "timestamp": float(stamp or 0.0),
    }


def _sniff(path):
    """Guess the real file type from its first bytes, whatever the extension says."""
    with open(path, "rb") as f:
        head = f.read(16)
    if head[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"avif"):
        return "HEIC"
    if head[:4] in (b"II*\0", b"MM\0*"):
        return "TIFF"  # DNG and most RAW formats are TIFF-based
    return None


def _camera_to_xyz(raw):
    """The camera's own camera-RGB -> XYZ (D65) matrix, from the file's color data.

    For DNG files LibRaw exposes the matrix as color_matrix: white-balanced
    camera RGB -> linear sRGB, with the daylight multipliers in
    daylight_whitebalance. Other formats may only have rgb_xyz_matrix
    (XYZ -> camera).
    """
    m = None
    cmx = np.array(raw.color_matrix, dtype=float)
    pm = np.array(raw.daylight_whitebalance, dtype=float)
    desc = raw.color_desc.decode("ascii")
    if np.abs(cmx[:, :3]).sum() > 0 and (pm[:3] > 0).all() and desc[:3] == "RGB":
        g2 = pm[3] if len(pm) > 3 and pm[3] > 0 else pm[1]
        col_g = cmx[:, 1] * pm[1] + (cmx[:, 3] * g2 if cmx.shape[1] > 3 else 0)
        m = SRGB_TO_XYZ @ np.column_stack([cmx[:, 0] * pm[0], col_g, cmx[:, 2] * pm[2]])
    else:
        xyz_to_cam = np.array(raw.rgb_xyz_matrix[:3], dtype=float)
        if abs(np.linalg.det(xyz_to_cam)) > 1e-9:
            m = np.linalg.inv(xyz_to_cam)
    if m is None or not np.isfinite(m).all() or abs(np.linalg.det(m)) < 1e-12:
        return None
    return m


SRGB_TO_XYZ = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
])
