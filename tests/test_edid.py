"""EDID parsing: identity and primaries come out, the serial number never does."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor_calibration import edid, privacy  # noqa: E402


def make_edid(manufacturer="DEL", product=0xA0C4, name="DELL U2722DE", serial=0x12345678, serial_text="CN0SERIAL99",
              year=2022, size=(2560, 1440), primaries=((0.640, 0.330), (0.300, 0.600), (0.150, 0.060)), white=(0.3127, 0.3290)):
    d = bytearray(128)
    d[:8] = edid.HEADER
    m = sum((ord(c) - 64) << s for c, s in zip(manufacturer, (10, 5, 0)))
    d[8], d[9] = m >> 8, m & 255
    d[10], d[11] = product & 255, product >> 8
    d[12:16] = serial.to_bytes(4, "little")
    d[16], d[17] = 12, year - 1990
    d[18], d[19] = 1, 4
    pts = [*primaries, white]
    raw = [round(v * 1024) for xy in pts for v in xy]  # rx ry gx gy bx by wx wy
    for i, r in enumerate(raw):
        d[27 + i] = r >> 2
    d[25] = sum((raw[i] & 3) << s for i, s in zip(range(4), (6, 4, 2, 0)))
    d[26] = sum((raw[4 + i] & 3) << s for i, s in zip(range(4), (6, 4, 2, 0)))
    w, h = size
    t = bytearray(18)  # detailed timing descriptor
    t[0], t[1] = 0x01, 0x1D
    t[2], t[4] = w & 255, (w >> 8) << 4
    t[5], t[7] = h & 255, (h >> 8) << 4
    d[54:72] = t
    # Descriptor text is 13 bytes: the text, then a newline and spaces (no newline when all 13 are used).
    d[72:90] = bytes([0, 0, 0, 0xFC, 0]) + (name + "\n").ljust(13)[:13].encode()
    d[90:108] = bytes([0, 0, 0, 0xFF, 0]) + (serial_text + "\n").ljust(13)[:13].encode()
    d[127] = (256 - sum(d[:127])) % 256
    return bytes(d)


class Edid(unittest.TestCase):
    def test_parse(self):
        m = edid.parse(make_edid())
        self.assertEqual(m["manufacturer"], "DEL")
        self.assertEqual(m["product_code"], 0xA0C4)
        self.assertEqual(m["model_name"], "DELL U2722DE")
        self.assertEqual(m["year"], 2022)
        self.assertEqual(m["resolution"], [2560, 1440])
        c = m["chromaticity"]
        self.assertAlmostEqual(c["rx"], 0.640, places=2)
        self.assertAlmostEqual(c["gy"], 0.600, places=2)
        self.assertAlmostEqual(c["wx"], 0.3127, places=2)

    def test_serial_number_is_never_returned(self):
        blob = make_edid(serial=0x5EC12E7, serial_text="CN0SERIAL99")
        self.assertIn(b"CN0SERIAL99", blob)
        m = edid.parse(blob)
        self.assertNotIn("CN0SERIAL99", repr(m))
        self.assertNotIn(str(0x5EC12E7), repr(m))
        self.assertEqual(set(m), {"manufacturer", "product_code", "model_name", "year", "chromaticity", "resolution"})
        self.assertEqual(set(privacy.clean_monitor(m)), set(m))

    def test_invalid_blocks(self):
        good = bytearray(make_edid())
        self.assertIsNone(edid.parse(b"\x00" * 128))
        self.assertIsNone(edid.parse(good[:100]))
        bad_sum = bytearray(good)
        bad_sum[127] ^= 0xFF
        self.assertIsNone(edid.parse(bad_sum))

    def test_read_all_dedupes_and_survives_errors(self):
        a, b = make_edid(), make_edid(manufacturer="AUS", product=0x27E5, name="ROG TESTMON01", serial=7)
        with mock.patch.object(edid.sys, "platform", "linux"), mock.patch.object(edid, "_linux", return_value=[a, a, b, b"junk"]):
            names = [m["model_name"] for m in edid.read_all()]
        self.assertEqual(names, ["DELL U2722DE", "ROG TESTMON01"])
        with mock.patch.object(edid, "_linux", side_effect=OSError), mock.patch.object(edid.sys, "platform", "linux"):
            self.assertEqual(edid.read_all(), [])

    def test_this_machine_does_not_crash(self):
        for m in edid.read_all():
            self.assertNotIn("serial", repr(m).lower())


if __name__ == "__main__":
    unittest.main()
