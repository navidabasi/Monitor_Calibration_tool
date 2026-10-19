"""End-to-end test on synthetic DNG photos.

    .venv\\Scripts\\python -m unittest discover -s tests -v
"""

import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from monitor_calibration import analyze, chart, detect, icc  # noqa: E402
from monitor_calibration import colorimetry as cm  # noqa: E402
from monitor_calibration.server import process_file  # noqa: E402
import synth  # noqa: E402

TARGET = synth.Display(synth.P3_TO_XYZ, gains=(0.97, 1.0, 1.06), gamma=2.35, luminance=1.2, crush=5)
TARGET_DIM = synth.Display(synth.P3_TO_XYZ, gains=(0.97, 1.0, 1.06), gamma=2.35, luminance=0.72, crush=5)
REF_P3 = synth.Display(synth.P3_TO_XYZ, gamma=2.2, luminance=0.9, black=0.001)
P3_PRIMARIES = [(0.680, 0.320), (0.265, 0.690), (0.150, 0.060)]
MAIN = ("ref.dng", "white.dng", "bright.dng", "dark.dng")


def make_icc(primaries, white, v4=True, name=b"Test monitor"):
    """A minimal matrix/shaper ICC display profile, adapted to D50 like real ones."""
    m = cm.rgb_to_xyz_matrix(primaries, white)
    to_pcs = icc.bradford(m.sum(axis=1), icc.D50)
    s15 = lambda vals: b"".join(struct.pack(">i", round(v * 65536)) for v in vals)  # noqa: E731
    xyz_tag = lambda v: b"XYZ \0\0\0\0" + s15(v)  # noqa: E731
    tags = {b"rXYZ": xyz_tag(to_pcs @ m[:, 0]), b"gXYZ": xyz_tag(to_pcs @ m[:, 1]), b"bXYZ": xyz_tag(to_pcs @ m[:, 2]),
            b"desc": b"desc\0\0\0\0" + struct.pack(">I", len(name) + 1) + name + b"\0"}
    if v4:
        tags[b"wtpt"] = xyz_tag(icc.D50)
        tags[b"chad"] = b"sf32\0\0\0\0" + s15(to_pcs.ravel())
    else:
        tags[b"wtpt"] = xyz_tag(m.sum(axis=1))
    header = bytearray(128)
    header[8] = 4 if v4 else 2
    header[12:16] = b"mntr"
    header[36:40] = b"acsp"
    table, body = struct.pack(">I", len(tags)), b""
    offset = 128 + 4 + 12 * len(tags)
    for sig, data in tags.items():
        table += struct.pack(">4sII", sig, offset + len(body), len(data))
        body += data + b"\0" * (-len(data) % 4)
    return bytes(header) + table + body
SHUTTER = 1 / 60
EXPOSURE = 0.32


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        ref = synth.reference_display()
        shots = [
            ("ref.dng", ref, "bright", "reference", 1.0, dict(offset=(-10, 8), angle=-1.0, frac=1.04), "21:00:00"),
            ("white.dng", TARGET, "white", "target", 1.0, {}, "21:01:00"),
            ("bright.dng", TARGET, "bright", "target", 1.0, {}, "21:02:00"),
            ("dark.dng", TARGET, "dark", "target", 15.0, {}, "21:03:00"),
            # same bright chart with the phone upside down: tests ID-based orientation
            ("bright_rot.dng", TARGET, "bright", "target", 1.0, dict(rot180=True), "21:04:00"),
            # dark chart after the monitor was dimmed by 40%
            ("dark_dim.dng", TARGET_DIM, "dark", "target", 15.0, {}, "21:05:00"),
            # a reference monitor calibrated to Display P3 instead of sRGB
            ("ref_p3.dng", REF_P3, "bright", "reference", 1.0, dict(offset=(5, 6), angle=0.8), "20:59:00"),
        ]
        cls.photos = []
        for i, (name, disp, typ, role, exp_mult, kw, t) in enumerate(shots, start=1):
            mosaic = synth.render(disp, typ, role, exposure=EXPOSURE * exp_mult, seed=i, **kw)
            path = d / name
            synth.write_dng(path, mosaic, SHUTTER * exp_mult, when=f"2026:10:18 {t}")
            result, error = process_file(path)
            cls.photos.append({"id": i, "name": name, "result": result, "error": error, "expect": (typ, role)})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_camera_values_positive(self):
        for prim in (synth.P3_TO_XYZ.T, cm.SRGB_TO_XYZ.T):
            for xyz in prim:
                self.assertTrue((synth.CAMERA @ xyz > 0).all())

    def test_detection(self):
        for p in self.photos:
            self.assertIsNone(p["error"], p["name"])
            self.assertEqual(analyze.identity(p), p["expect"], p["name"])
        rot = next(p for p in self.photos if p["name"] == "bright_rot.dng")
        self.assertEqual(rot["result"]["rotation"], 2)
        for p in self.photos:
            self.assertFalse(any("overexposed" in w for w in p["result"]["warnings"]), p["result"]["warnings"])

    def _near_black_ok(self, nb):
        steps = {s["code"]: s for s in nb["steps"]}
        self.assertIn(steps[3]["status"], ("crushed", "faint"))
        self.assertEqual(steps[5]["status"], "crushed")
        for code in (13, 20, 26, 38):
            self.assertEqual(steps[code]["status"], "ok", code)
            self.assertLess(abs(steps[code]["ratio"] - (code / 255) ** 2.35 / (code / 255) ** 2.2), 0.12, code)

    def test_dark_shot_after_brightness_change(self):
        photos = [p for p in self.photos if p["name"] not in ("bright_rot.dng", "dark.dng")]
        a = analyze.analyze_session(photos, "2.2")
        self.assertTrue(a["nearBlack"]["selfReferenced"])
        self._near_black_ok(a["nearBlack"])

    def _photos(self, *names):
        return [p for p in self.photos if p["name"] in names]

    def test_gains(self):
        # The simulated monitor's gains are (0.97, 1, 1.06): the fix is 1/gain, scaled so the largest is 1.
        r = analyze.analyze_session(self._photos(*MAIN), "2.2")["rounds"][0]
        want = 1 / np.array([0.97, 1.0, 1.06])
        want /= want.max()
        print("gains", r["gains"], "want", want.round(3))
        self.assertEqual(r["gains"]["method"], "measured")
        self.assertLess(np.abs(np.array(r["gains"]["k"]) - want).max(), 0.02)
        self.assertAlmostEqual(sum(r["gains"]["y"]), 1.0, delta=0.05)
        # The basis adds up to the measured white, and the suggested gains land on D65.
        basis = np.array(r["gains"]["basis"]).T
        white = cm.xy_to_xyz(r["white"]["xy"])
        self.assertLess(np.abs(basis.sum(axis=1) - white).max(), 1e-6)
        landed = basis @ np.array(r["gains"]["k"])
        self.assertLess(np.abs(np.array(cm.xy(landed)) - (0.3127, 0.3290)).max(), 0.0005)
        # Too cool and greenish-neutral: offset points toward cooler.
        self.assertLess(r["white"]["offset"]["warm"], 0)

    def test_p3_reference(self):
        photos = self._photos("ref_p3.dng", "white.dng", "bright.dng", "dark.dng")
        want_cct, _ = cm.cct_duv(TARGET.white)
        for spec in ({"name": "p3"}, {"kind": "custom", "name": "P3 by hand", "primaries": P3_PRIMARIES, "white": [0.3127, 0.329]}):
            a = analyze.analyze_session(photos, "2.2", ref_spec=spec)
            self.assertEqual(a["method"], "reference")
            self.assertLess(abs(a["rounds"][0]["white"]["cct"] - want_cct), 60, spec)
            self.assertLess(abs(a["rounds"][0]["gamut"] - 1.36), 0.05, spec)
        # Calling the P3 monitor sRGB barely moves white (same D65 white, weighted most),
        # but it gets the colors wrong, so the choice matters for gamut.
        wrong = analyze.analyze_session(photos, "2.2", ref_spec={"name": "srgb"})
        self.assertLess(abs(wrong["rounds"][0]["white"]["cct"] - want_cct), 150)
        self.assertGreater(abs(wrong["rounds"][0]["gamut"] - 1.36), 0.15)

    def test_invalid_custom_reference_falls_back(self):
        a = analyze.analyze_session(self._photos(*MAIN), "2.2", ref_spec={"kind": "custom", "primaries": [[2, 2]] * 3, "white": [0, 0]})
        self.assertEqual(a["referenceSpec"]["name"], "sRGB")
        self.assertTrue(any("custom reference" in w for w in a["warnings"]))

    def test_icc_parse(self):
        for v4 in (True, False):
            prof = icc.parse(make_icc(P3_PRIMARIES, (0.3127, 0.329), v4=v4))
            self.assertEqual(prof["name"], "Test monitor")
            for got, want in zip(prof["primaries"], P3_PRIMARIES):
                self.assertLess(max(abs(got[0] - want[0]), abs(got[1] - want[1])), 0.002, (v4, got, want))
            self.assertLess(abs(prof["white"][0] - 0.3127) + abs(prof["white"][1] - 0.329), 0.002)
        warm = icc.parse(make_icc(cm.SRGB_PRIMARIES_XY, (0.3221, 0.3318)))  # ~6000 K white
        self.assertLess(abs(warm["cct"] - 6000), 60)
        with self.assertRaises(icc.ICCError):
            icc.parse(b"not a profile" * 20)

    def test_analysis(self):
        photos = self._photos(*MAIN)
        a = analyze.analyze_session(photos, "2.2", {"reference": "Dell", "target": "ASUS"})
        self.assertEqual(a["method"], "reference")
        self.assertEqual(len(a["rounds"]), 1)
        r = a["rounds"][0]

        want_cct, want_duv = cm.cct_duv(TARGET.white)
        print(f"\nwhite: measured {r['white']['cct']:.0f} K duv {r['white']['duv']:+.4f}, "
              f"expected {want_cct:.0f} K duv {want_duv:+.4f}")
        print(f"gamma {r['gamma']:.3f} (expected 2.35), brightness {r['brightness']:.3f} (expected 1.2), "
              f"gamut {r['gamut']:.2f}")
        self.assertLess(abs(r["white"]["cct"] - want_cct), 60)
        self.assertLess(abs(r["white"]["duv"] - want_duv), 0.0015)
        self.assertLess(abs(r["gamma"] - 2.35), 0.04)
        self.assertLess(abs(r["brightness"] - 1.2), 0.04)
        p3_ratio = cm.triangle_area([(0.68, 0.32), (0.265, 0.69), (0.15, 0.06)]) / cm.triangle_area(cm.SRGB_PRIMARIES_XY)
        self.assertLess(abs(r["gamut"] - p3_ratio), 0.05)

        # Reference monitor reads as accurate.
        self.assertLess(a["reference"]["maxTint"], 1.5)
        self.assertLess(abs(a["reference"]["gamma"] - 2.2), 0.04)

        # Codes 3 and 5 are crushed to black by the simulated display.
        print("near-black:", [(s["code"], s["status"], None if s["ratio"] is None else round(s["ratio"], 2)) for s in a["nearBlack"]["steps"]])
        self._near_black_ok(a["nearBlack"])

        titles = [x["title"] for x in a["advice"]]
        print("advice:", titles)
        self.assertTrue(any("White is" in t for t in titles))
        self.assertTrue(any("Midtones are too dark" in t for t in titles))
        self.assertTrue(any("wider than sRGB" in t for t in titles))
        self.assertTrue(any("darkest shades" in t for t in titles))
        self.assertTrue(any("brighter than" in t for t in titles))

        # The camera's own matrix agrees (synthetic camera has no spectral mismatch).
        self.assertLess(abs(a["crossCheck"]["cct"] - want_cct), 150)

    def test_rotated_round_matches(self):
        photos = [p for p in self.photos if p["name"] != "bright.dng"]
        a = analyze.analyze_session(photos, "2.2")
        r = a["rounds"][0]
        self.assertLess(abs(r["gamma"] - 2.35), 0.05)

    def test_no_reference(self):
        photos = [p for p in self.photos if p["expect"][1] == "target" and p["name"] != "bright_rot.dng"]
        a = analyze.analyze_session(photos, "2.2")
        self.assertEqual(a["method"], "camera")
        want_cct, _ = cm.cct_duv(TARGET.white)
        self.assertLess(abs(a["rounds"][0]["white"]["cct"] - want_cct), 150)
        # Without a reference it must not tell you how far to move the RGB gains.
        details = " ".join(x["detail"] for x in a["advice"])
        self.assertNotIn("lower Red", details)
        self.assertNotIn("lower Blue", details)
        self.assertTrue(any("don't adjust RGB gains" in x["title"] for x in a["advice"]))

    def test_unchanged_white_between_rounds(self):
        # bright.dng and bright_rot.dng show the same display: white didn't move.
        a = analyze.analyze_session(self.photos, "2.2")
        self.assertEqual(len(a["rounds"]), 2)
        self.assertTrue(any("hasn't changed color" in w for w in a["warnings"]), a["warnings"])
        # A display with no gray-scale tint must not get a tint warning.
        self.assertFalse(any("tint" in x["title"] for x in a["advice"]))

    def test_blank_photo(self):
        with self.assertRaises(detect.ChartNotFound):
            detect.measure(np.zeros((600, 800, 3), np.float32), {})

    def test_exposure_tags_read_from_file(self):
        from monitor_calibration import tiff_meta
        path = Path(self.tmp.name) / "tags.dng"
        synth.write_dng(path, np.full((8, 8), 100, np.uint16), 1 / 125, iso=80, when="2026:10:18 21:30:00")
        tags = tiff_meta.read_exposure(path)
        self.assertAlmostEqual(tags["shutter"], 1 / 125, places=6)
        self.assertEqual(tags["iso"], 80)
        self.assertGreater(tags["timestamp"], 0)
        dark = next(p for p in self.photos if p["name"] == "dark.dng")
        self.assertAlmostEqual(dark["result"]["exposure"]["shutter"], 15 / 60, places=5)
        # Not a TIFF, and a truncated TIFF: no crash.
        junk = Path(self.tmp.name) / "junk.dng"
        junk.write_bytes(b"\x00" * 64)
        self.assertIsNone(tiff_meta.read_exposure(junk))
        junk.write_bytes(path.read_bytes()[:40])
        self.assertEqual(tiff_meta.read_exposure(junk)["shutter"], 0.0)

    def test_jpeg_named_dng(self):
        path = Path(self.tmp.name) / "IMG_0001.DNG"
        path.write_bytes(b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00" + b"\x00" * 64)
        result, error = process_file(path)
        self.assertIsNone(result)
        self.assertIn("JPEG image, not a RAW photo", error)

    def test_bits_roundtrip(self):
        for t in chart.TYPES:
            for r in chart.ROLES:
                self.assertEqual(chart.decode_bits(chart.encode_bits(t, r)), (t, r))


if __name__ == "__main__":
    unittest.main()
