"""Phase 2: a reference session teaches the app about the phone; a later session without a reference uses it."""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from monitor_calibration import analyze, profiles, server  # noqa: E402
from monitor_calibration import colorimetry as cm  # noqa: E402
import synth  # noqa: E402

# A phone whose embedded color data doesn't match its sensor, like a real one under OLED light.
EMBEDDED = synth.CAMERA @ np.array([[1.02, 0.0, -0.03], [0.0, 1.0, 0.0], [-0.02, 0.0, 1.04]])
REF = synth.reference_display()
TARGET_A = synth.Display(synth.P3_TO_XYZ, gains=(0.97, 1.0, 1.06), gamma=2.35, luminance=1.2, crush=5)
TARGET_B = synth.Display(synth.P3_TO_XYZ, gains=(1.04, 1.0, 0.95), gamma=2.1, luminance=0.8, crush=2)
PHONE = dict(make="Apple", model="iPhone Test", unique="iPhone Test back camera", software="Adobe Photoshop Lightroom 9.0",
             lens="iPhone Test back camera 6.765mm f/1.78")


class Profiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.n = 0
        cls.session_a = [cls.shot("ref.dng", REF, "bright", "reference"), cls.shot("white.dng", TARGET_A, "white", "target"),
                         cls.shot("bright.dng", TARGET_A, "bright", "target")]
        cls.session_b = [cls.shot("white_b.dng", TARGET_B, "white", "target"), cls.shot("bright_b.dng", TARGET_B, "bright", "target")]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def shot(cls, name, display, typ, role):
        cls.n += 1
        mosaic = synth.render(display, typ, role, exposure=0.32, seed=cls.n)
        path = cls.dir / name
        synth.write_dng(path, mosaic, 1 / 60, color_matrix=EMBEDDED, **PHONE)
        result, error = server.process_file(path)
        assert error is None, error
        return {"id": cls.n, "name": name, "result": result}

    def test_reference_session_learns_a_correction(self):
        a = analyze.analyze_session(self.session_a, "2.2")
        c = a["correction"]
        self.assertTrue(c["good"], c["problems"])
        self.assertTrue(analyze.valid_correction(c["k"]))
        # The correction is a modest change on top of the phone's own matrix.
        self.assertLess(np.abs(np.array(c["k"]) - np.eye(3)).max(), 0.2)

    def test_no_reference_session_is_accurate_with_the_correction(self):
        truth, _ = cm.cct_duv(TARGET_B.white)
        plain = analyze.analyze_session(self.session_b, "2.2")
        self.assertEqual(plain["method"], "camera")
        plain_err = abs(plain["rounds"][0]["white"]["cct"] - truth)
        self.assertGreater(plain_err, 150)  # the phone's own data is off, so the correction has something to fix

        k = analyze.analyze_session(self.session_a, "2.2")["correction"]["k"]
        fixed = analyze.analyze_session(self.session_b, "2.2", correction={"k": k, "label": "x", "source": "local"})
        self.assertEqual(fixed["method"], "profile")
        err = abs(fixed["rounds"][0]["white"]["cct"] - truth)
        print(f"\nwhite error: phone data {plain_err:.0f} K, with saved correction {err:.0f} K")
        self.assertLess(err, 60)
        titles = " ".join(x["title"] for x in fixed["advice"])
        self.assertNotIn("don't adjust RGB gains", titles)
        self.assertIn("saved camera correction", titles)

    def test_bad_corrections_are_ignored(self):
        for bad in ({"k": [[1, 0, 0], [0, 1, 0]]}, {"k": [[1e9, 0, 0], [0, 1, 0], [0, 0, 1e-9]]},
                    {"k": [[float("nan")] * 3] * 3}, {"k": [[-1, 0, 0], [0, 1, 0], [0, 0, 1]]}, {}):
            a = analyze.analyze_session(self.session_b, "2.2", correction=bad)
            self.assertEqual(a["method"], "camera", bad)

    def test_reference_session_ignores_a_saved_correction(self):
        bogus = {"k": [[2, 0, 0], [0, 1, 0], [0, 0, 0.5]], "label": "x", "source": "local"}
        a = analyze.analyze_session(self.session_a, "2.2", correction=bogus)
        self.assertEqual(a["method"], "reference")

    def test_profile_files(self):
        folder = self.dir / "profiles"
        phone = {"unique_camera_model": "P", "lens_model": "", "raw_type": "bayer", "app_family": "lightroom"}
        k = np.eye(3).tolist()
        self.assertFalse(profiles.save(folder, phone, "woled", None, [[1, 2, 3]]))
        self.assertTrue(profiles.save_for_both(folder, phone, "AUS:1", "woled", k))
        self.assertEqual(profiles.find(folder, phone, "woled", None)["source"], "local")
        self.assertIsNotNone(profiles.find(folder, phone, "woled", "AUS:1"))
        # A different kind of screen, another phone or another camera app never match.
        self.assertIsNone(profiles.find(folder, phone, "qdoled", None))
        self.assertIsNone(profiles.find(folder, dict(phone, app_family="open_camera"), "woled", None))
        self.assertIsNone(profiles.find(folder, dict(phone, unique_camera_model="Q"), "woled", "AUS:1"))
        # The exact monitor model beats the technology.
        better = (np.eye(3) * 1.01).tolist()
        profiles.save(folder, phone, "woled", "AUS:1", better)
        self.assertEqual(profiles.find(folder, phone, "woled", "AUS:1")["k"], better)
        self.assertEqual(profiles.find(folder, phone, "woled", "AUS:2")["k"], k)
        (next(folder.glob("*.json"))).write_text("not json")
        profiles.find(folder, phone, "woled", None)  # damaged files don't crash

    def test_server_saves_and_reuses_the_profile(self):
        data = self.dir / "app"
        server.SESSION = data / "current"
        woled = {"manufacturer": "DEL", "product_code": 7, "screen_tech": "woled"}
        names_a = {"monitors": {"reference": woled, "target": {"screen_tech": "woled"}}}
        out = server._session_json({"photos": self.session_a}, "2.2", names_a)
        self.assertTrue(out["analysis"]["correction"]["saved"])
        self.assertNotIn("k", out["analysis"]["correction"])
        self.assertIn("lightroom", out["analysis"]["phone"]["label"])
        self.assertTrue(any((data / "profiles").glob("*.json")))

        names_b = {"monitors": {"reference": {}, "target": {"screen_tech": "woled"}}}
        out = server._session_json({"photos": self.session_b}, "2.2", names_b)
        self.assertEqual(out["analysis"]["method"], "profile")
        self.assertIn("saved", out["analysis"]["profile"]["label"])
        # A screen of another kind gets nothing.
        other = server._session_json({"photos": self.session_b}, "2.2", {"monitors": {"target": {"screen_tech": "qdoled"}}})
        self.assertEqual(other["analysis"]["method"], "camera")
        # Technology unknown: nothing is saved or reused.
        unknown = server._session_json({"photos": self.session_b}, "2.2", {"monitors": {"target": {}}})
        self.assertEqual(unknown["analysis"]["method"], "camera")


if __name__ == "__main__":
    unittest.main()
