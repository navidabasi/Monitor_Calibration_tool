"""Phone metadata from DNG tags: only allow-listed fields ever come out."""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from monitor_calibration import privacy, tiff_meta  # noqa: E402
from monitor_calibration.server import process_file  # noqa: E402
import synth  # noqa: E402

SECRETS = ["SN-12345-BODY", "SN-LENS-777", "Jane Q. Owner", "C:\\Users\\jane\\Pictures", "IMG_SECRET.DNG", "jane@example.com"]


def _identifying_extras():
    """Tags that identify a person, a place or a device. None may ever be read."""
    ifd0 = [
        (315, 2, "Jane Q. Owner"),                      # Artist
        (33432, 2, "(c) jane@example.com"),             # Copyright
        (50735, 2, "SN-12345-BODY"),                    # CameraSerialNumber (DNG)
        (50827, 1, list(b"C:\\Users\\jane\\Pictures\\IMG_SECRET.DNG")),  # OriginalRawFileName
        (50781, 1, list(range(16))),                    # RawDataUniqueID
        (34853, 4, [0]),                                # GPSInfo pointer
        (270, 2, "Jane's desk monitor test"),           # ImageDescription
        (306, 2, "2026:10:18 21:00:00"),                # DateTime
    ]
    exif = [
        (42033, 2, "SN-12345-BODY"),                    # BodySerialNumber
        (42037, 2, "SN-LENS-777"),                      # LensSerialNumber
        (42032, 2, "Jane Q. Owner"),                    # CameraOwnerName
        (42016, 2, "0123456789abcdef0123456789abcdef"),  # ImageUniqueID
        (37500, 7, list(b"MAKERNOTE-CONTENT-ID-SECRET")),  # MakerNote
        (37510, 7, list(b"user comment jane@example.com")),  # UserComment
    ]
    return ifd0, exif


class Capture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _dng(self, name="a.dng", **kw):
        path = self.dir / name
        synth.write_dng(path, np.full((8, 8), 100, np.uint16), 1 / 60, iso=64, **kw)
        return path

    def test_allow_listed_fields_are_read(self):
        path = self._dng(make="Apple", model="iPhone 17 Pro", unique="iPhone17,2 back camera",
                         software="Adobe Photoshop Lightroom 9.4.1 (iOS)", lens="iPhone 17 Pro back camera 6.765mm f/1.78")
        cap = privacy.clean_capture(tiff_meta.read_capture(path))
        self.assertEqual(cap["Make"], "Apple")
        self.assertEqual(cap["UniqueCameraModel"], "iPhone17,2 back camera")
        self.assertEqual(cap["LensModel"], "iPhone 17 Pro back camera 6.765mm f/1.78")
        self.assertAlmostEqual(cap["FocalLength"], 6.765, places=3)
        self.assertEqual(cap["RawType"], "bayer")
        self.assertEqual(cap["CFAPattern"], [0, 1, 1, 2])
        self.assertEqual(cap["BlackLevel"], [synth.BLACK])
        self.assertEqual(cap["WhiteLevel"], synth.WHITE)
        self.assertEqual(cap["DNGVersion"], [1, 4, 0, 0])
        self.assertEqual(len(cap["ColorMatrix1"]), 9)
        self.assertAlmostEqual(cap["ColorMatrix1"][0], float(synth.CAMERA[0, 0]), places=5)
        self.assertEqual(cap["CalibrationIlluminant1"], 21)
        self.assertEqual(cap["ISOSpeedRatings"], 64)
        self.assertEqual((cap["ImageWidth"], cap["ImageLength"]), (8, 8))
        # Software is reduced to the app and its version: no OS, paths or other text.
        self.assertEqual(cap["Software"], "lightroom 9.4.1")

    def test_identifying_tags_are_never_read(self):
        ifd0, exif = _identifying_extras()
        path = self._dng("id.dng", extra_ifd0=ifd0, extra_exif=exif, software="Open Camera 1.53.1 jane@example.com")
        raw = tiff_meta.read_capture(path)
        allowed = set(tiff_meta.CAPTURE_TAGS.values()) | {"RawType"}
        self.assertLessEqual(set(raw), allowed)
        cap = privacy.clean_capture(raw)
        self.assertLessEqual(set(cap), privacy.CAPTURE_FIELDS)
        text = repr(raw.keys()) + repr(cap)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        # The serial and owner text is in the file, so the test is meaningful.
        self.assertIn(b"SN-12345-BODY", path.read_bytes())
        self.assertEqual(cap["Software"], "open_camera 1.53.1")

    def test_cleaning_drops_unknown_and_bad_values(self):
        cap = privacy.clean_capture({
            "Make": "Apple", "Model": "x" * 500, "UniqueCameraModel": "C:\\Users\\jane\\cam",
            "BodySerialNumber": "SN-1", "GPSLatitude": [52.0], "Artist": "Jane",
            "FocalLength": float("nan"), "ISOSpeedRatings": True, "ColorMatrix1": [1, 2, 3],
            "BlackLevel": [64, "x"], "RawType": "weird", "Orientation": 1,
        })
        self.assertEqual(set(cap), {"Make", "Model", "Software", "Orientation"})
        self.assertEqual(len(cap["Model"]), 60)
        self.assertEqual(privacy.clean_capture(None), {"Software": "other"})

    def test_not_a_tiff_and_damaged_files(self):
        junk = self.dir / "junk.dng"
        junk.write_bytes(b"\x00" * 64)
        self.assertIsNone(tiff_meta.read_capture(junk))
        good = self._dng("good.dng", lens="lens")
        junk.write_bytes(good.read_bytes()[:60])
        self.assertIsInstance(tiff_meta.read_capture(junk), dict)  # no crash

    def test_capture_reaches_the_analysis_result(self):
        path = self.dir / "chart.dng"
        mosaic = synth.render(synth.reference_display(), "bright", "reference", exposure=0.32, seed=1)
        ifd0, exif = _identifying_extras()
        synth.write_dng(path, mosaic, 1 / 60, unique="Synthetic cam", extra_ifd0=ifd0, extra_exif=exif)
        result, error = process_file(path)
        self.assertIsNone(error)
        self.assertEqual(result["capture"]["UniqueCameraModel"], "Synthetic cam")
        for secret in SECRETS:
            self.assertNotIn(secret, repr(result))

    def test_phone_key(self):
        cap = {"UniqueCameraModel": "iPhone17,2 back camera", "LensModel": "main", "RawType": "bayer", "Software": "lightroom 9.4"}
        key = privacy.phone_key(cap)
        self.assertEqual(key, {"unique_camera_model": "iPhone17,2 back camera", "lens_model": "main",
                               "raw_type": "bayer", "app_family": "lightroom"})
        self.assertEqual(privacy.phone_label(key), "iPhone17,2 · lightroom bayer RAW")
        self.assertIsNone(privacy.phone_key({"Make": "x"}))


if __name__ == "__main__":
    unittest.main()
