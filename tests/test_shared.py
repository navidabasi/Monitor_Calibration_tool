"""Phase 4, end to end: the local app, a real HTTP connection and the hosted API (in memory).

One person shares a reference session, two others do too (injected as if from other
networks), the correction is published, and a fourth person with the same phone and no
reference monitor gets an accurate white point from it. Then the first person deletes
their contribution.
"""

import copy
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from api.v1._lib import aggregate  # noqa: E402
from dev_api import make_dev_server  # noqa: E402
from monitor_calibration import analyze, server, shared, submission  # noqa: E402
from monitor_calibration import colorimetry as cm  # noqa: E402
import synth  # noqa: E402
from test_api import DAY, EMBEDDED, PHONE, Base, FakeClock  # noqa: E402
from test_reference_profiles import Helpers as ReferenceHelpers  # noqa: E402

OTHER_DISPLAY = synth.Display(synth.P3_TO_XYZ, gains=(1.04, 1.0, 0.95), gamma=2.1, luminance=0.8, crush=2)
MONITORS = {"reference": {"manufacturer": "DEL", "product_code": 41156, "model_name": "DELL U2722DE", "screen_tech": "ips_wled"},
            "target": {"screen_tech": "ips_wled", "picture_mode": "srgb"}}


class App:
    """A running local app plus a way to talk to it the way the page does."""

    def __init__(self, data_dir):
        self.token = ""
        self.srv = server.make_server(0, data_dir)
        self.session = server.SESSION  # the server keeps its folder in a module global, so each call re-selects its own
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.token = re.search(r'name="mc-token" content="([^"]+)"', self.call("GET", "/")[1]).group(1)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method, path, body=None, json_body=None):
        server.SESSION = self.session
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        data = body if body is not None else (json.dumps(json_body).encode() if json_body is not None else b"")
        conn.putrequest(method, path, skip_host=True)
        for k, v in {"Host": f"127.0.0.1:{self.port}", "X-MC-Token": self.token, "Sec-Fetch-Site": "same-origin",
                     "Origin": f"http://127.0.0.1:{self.port}", "Content-Length": str(len(data))}.items():
            conn.putheader(k, v)
        conn.endheaders(data)
        res = conn.getresponse()
        text = res.read().decode("utf-8", "replace")
        conn.close()
        return res.status, text

    def json(self, method, path, **kw):
        status, text = self.call(method, path, **kw)
        return status, json.loads(text)

    def upload(self, path, query):
        return self.json("POST", f"/api/upload?name={path.name}&{query}", body=path.read_bytes())


class SharedEndToEnd(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # What a person with the same phone and a plain IPS monitor photographs, without a reference monitor.
        cls.other = [cls.shot("white_o.dng", OTHER_DISPLAY, "white", "target"), cls.shot("bright_o.dng", OTHER_DISPLAY, "bright", "target")]
        # Other people whose monitor ended up on target, for the shared-settings test.
        good = cls.shot("bright_good.dng", synth.reference_display(), "bright", "target")
        cls.good_charts = submission.build([cls.photos[0], cls.photos[1], good], {"name": "srgb"}, need_round=True)[0]
        cls.query = "target=2.2&mon=" + json.dumps(MONITORS).replace(" ", "%20").replace('"', "%22").replace("{", "%7B").replace("}", "%7D")

    def setUp(self):
        self.clock = FakeClock()
        self.api, self.ctx = make_dev_server(0, difficulty=8, clock=self.clock)
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        env = mock.patch.dict(os.environ, {"MONCAL_API_URL": f"http://127.0.0.1:{self.api.server_address[1]}/api/v1"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)
        shared.forget_lookups()
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            app.close()

    def new_app(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = App(tmp.name)
        self.apps.append(app)
        return app

    def test_whole_flow(self):
        app = self.new_app()
        for name in ("ref.dng", "white.dng", "bright.dng"):
            status, session = app.upload(self.dir / name, self.query)
            self.assertEqual(status, 200)
        analysis = session["analysis"]
        self.assertEqual(analysis["method"], "reference")
        self.assertTrue(analysis["correction"]["good"])
        self.assertIn("lightroom", analysis["phone"]["label"])

        # Nobody has been asked yet: nothing is sent.
        self.assertEqual(app.json("GET", "/api/settings")[1], {"share": None, "apiConfigured": True})
        self.assertEqual(app.json("POST", "/api/share?" + self.query)[1], {"status": "off"})
        self.assertFalse(self.ctx.store.submissions)

        # Agree, then the session is shared once.
        self.assertEqual(app.json("POST", "/api/settings", json_body={"share": True})[1]["share"], True)
        self.assertEqual(app.json("POST", "/api/settings", json_body={"share": "yes"})[0], 400)
        status, answer = app.json("POST", "/api/share?" + self.query)
        self.assertEqual(answer["status"], "shared", answer)
        self.assertEqual(app.json("POST", "/api/share?" + self.query)[1], {"status": "already"})
        self.assertEqual(len(self.ctx.store.submissions), 1)
        sent = next(iter(self.ctx.store.submissions.values()))
        self.assertEqual(sent["reference_screen_tech"], "ips_wled")
        # What was sent: numbers only. No file names, no times, no photo content, no nickname.
        text = json.dumps(sent, default=str)
        for needle in ("ref.dng", "white.dng", ".dng", "timestamp", "uploaded", "sessions"):
            self.assertNotIn(needle, text)
        items = app.json("GET", "/api/contributions")[1]["items"]
        self.assertEqual(len(items), 1)
        self.assertNotIn("sig", items[0])

        # Two more people with the same phone, from other networks and days; the hourly job publishes the correction.
        rng = np.random.default_rng(21)
        for i in range(2):
            self.clock.t += DAY
            (status, *_), _ = self.submit(self.ctx, ip=f"10.{50 + i}.0.1", charts=self.jitter(rng), tech="ips_wled")
            self.assertEqual(status, 200)
        self.clock.t += DAY
        self.assertEqual(aggregate.run(self.ctx)["accepted"], 3)

        # A fourth person, same phone, no reference monitor, nothing saved on their computer.
        other = self.new_app()
        for p in self.other:
            status, session = other.upload(self.dir / p["name"], self.query)
        truth, _ = cm.cct_duv(OTHER_DISPLAY.white)
        plain = session["analysis"]
        self.assertEqual(plain["sharedState"], "off")  # they haven't agreed to share, so the app didn't ask the database
        self.assertEqual(plain["method"], "camera")
        plain_error = abs(plain["rounds"][0]["white"]["cct"] - truth)
        self.assertEqual(other.json("POST", "/api/settings", json_body={"share": True})[0], 200)
        status, session = other.json("GET", "/api/session?" + self.query)
        analysis = session["analysis"]
        self.assertEqual((analysis["method"], analysis["sharedState"]), ("profile", "found"))
        self.assertEqual(analysis["profile"]["source"], "shared")
        self.assertEqual(analysis["profile"]["contributors"], 3)
        error = abs(analysis["rounds"][0]["white"]["cct"] - truth)
        print(f"\nwhite error without reference: phone data {plain_error:.0f} K, shared correction {error:.0f} K")
        self.assertGreater(plain_error, 150)
        self.assertLess(error, 80)

        # The first person deletes their contribution; the correction drops below three contributors and disappears.
        status, answer = app.json("POST", f"/api/contributions/{items[0]['id']}/delete")
        self.assertEqual((status, answer), (200, {"items": []}))
        self.assertEqual(len(self.ctx.store.submissions), 2)
        shared.forget_lookups()
        status, session = other.json("GET", "/api/session?" + self.query)
        self.assertEqual((session["analysis"]["method"], session["analysis"]["sharedState"]), ("camera", "none"))
        self.assertEqual(app.json("POST", "/api/contributions/00000000/delete")[0], 404)

    def test_settings_are_asked_for_shared_and_suggested_only_when_enough_agree(self):
        mons = {"reference": MONITORS["reference"],
                "target": {"manufacturer": "AUS", "product_code": 43537, "model_name": "XG27AQDMES", "screen_tech": "ips_wled",
                           "picture_mode": "srgb"}}
        query = "target=2.2&mon=" + json.dumps(mons).replace(" ", "%20").replace('"', "%22").replace("{", "%7B").replace("}", "%7D")
        app = self.new_app()
        for name in ("ref.dng", "white.dng", "bright.dng"):
            app.upload(self.dir / name, query)
        settings = {"calibrated": True, "rgb": {"r": 97, "g": 100, "b": 94, "max": 100}}

        # Not agreed to share yet: nothing is asked of the database, nothing is sent.
        self.assertEqual(app.json("GET", "/api/recipe?" + query)[1], {"state": "off"})
        self.assertEqual(app.json("POST", "/api/share-settings?" + query, json_body=settings)[1], {"status": "off"})
        app.json("POST", "/api/settings", json_body={"share": True})
        for bad in ({}, {"calibrated": True, "rgb": {"r": 120, "g": 100, "b": 94, "max": 100}}, {"calibrated": "yes", "rgb": settings["rgb"]}):
            self.assertEqual(app.json("POST", "/api/share-settings?" + query, json_body=bad)[0], 400)
        self.assertFalse(self.ctx.store.settings_samples)

        # Without a monitor model or picture mode there is nothing to file the numbers under.
        plain = "target=2.2&mon=" + json.dumps({"reference": MONITORS["reference"], "target": {"screen_tech": "ips_wled"}}).replace(" ", "%20")
        self.assertEqual(app.json("GET", "/api/recipe?" + plain)[1], {"state": "unknown"})
        self.assertEqual(app.json("POST", "/api/share-settings?" + plain, json_body=settings)[1], {"status": "nothing"})

        # The person answers: calibrated, these are the values. It is sent once, with the server measuring the result.
        status, answer = app.json("POST", "/api/share-settings?" + query, json_body=settings)
        self.assertEqual(answer["status"], "shared", answer)
        self.assertEqual(app.json("POST", "/api/share-settings?" + query, json_body=settings)[1], {"status": "already"})
        row = next(iter(self.ctx.store.settings_samples.values()))
        self.assertTrue(row["calibrated"])
        self.assertEqual((row["rgb_r"], row["rgb_b"]), (97, 94))
        items = app.json("GET", "/api/contributions")[1]["items"]
        self.assertIn("calibrated", items[-1]["label"])

        # One person is not enough to suggest anything to anyone.
        self.assertEqual(app.json("GET", "/api/recipe?" + query)[1], {"state": "none", "recipe": None})

        # Five more people with the same monitor and picture mode, on different days, who reached the target.
        for i in range(5):
            self.clock.t += DAY
            (status, *_), _ = self.submit(self.ctx, ip=f"10.{60 + i}.0.1", charts=copy.deepcopy(self.good_charts), settings=dict(settings))
            self.assertEqual(status, 200)
        shared.forget_lookups()
        state = app.json("GET", "/api/recipe?" + query)[1]
        self.assertEqual(state["state"], "found")
        self.assertEqual(state["recipe"]["rgb"], [0.97, 1.0, 0.94])
        self.assertGreaterEqual(state["recipe"]["contributors"], 5)

        # Deleting your contribution removes the sample too.
        before = len(self.ctx.store.settings_samples)
        app.json("POST", f"/api/contributions/{items[-1]['id']}/delete")
        self.assertEqual(len(self.ctx.store.settings_samples), before - 1)

    def test_reference_profile_is_offered_only_when_enough_people_agree(self):
        mons = {"reference": {"manufacturer": "DEL", "product_code": 41156, "model_name": "DELL U2722DE", "screen_tech": "ips_wled",
                              "picture_mode": "srgb"}, "target": {"screen_tech": "ips_wled"}}
        quote = lambda d: json.dumps(d).replace(" ", "%20").replace('"', "%22").replace("{", "%7B").replace("}", "%7D")  # noqa: E731
        query = "target=2.2&mon=" + quote(mons)
        no_mode = "target=2.2&mon=" + quote({"reference": {k: v for k, v in mons["reference"].items() if k != "picture_mode"}})
        app = self.new_app()
        self.assertEqual(app.json("GET", "/api/reference-profile?" + query)[1], {"state": "off"})  # not agreed to share: no request
        app.json("POST", "/api/settings", json_body={"share": True})
        self.assertEqual(app.json("GET", "/api/reference-profile?" + no_mode)[1], {"state": "unknown"})
        self.assertEqual(app.json("GET", "/api/reference-profile?" + query)[1], {"state": "none", "profile": None})

        rng = np.random.default_rng(31)
        spec = lambda: {"kind": "custom", "primaries": [[round(x + rng.normal(0, 0.0008), 4), round(y + rng.normal(0, 0.0008), 4)]  # noqa: E731
                                                       for x, y in [[0.64, 0.33], [0.30, 0.60], [0.15, 0.06]]],
                        "white": [0.3127, 0.329]}
        for i in range(5):
            self.clock.t += DAY
            self.assertEqual(ReferenceHelpers.send(self, self.ctx, f"10.{70 + i}.0.1", spec()), 200)
        shared.forget_lookups()
        state = app.json("GET", "/api/reference-profile?" + query)[1]
        self.assertEqual(state["state"], "found")
        profile = state["profile"]
        self.assertEqual((profile["contributors"], profile["spec"]["source"]), (5, "shared"))
        self.assertAlmostEqual(profile["spec"]["primaries"][0][0], 0.64, delta=0.003)

        # Using those values as the reference is reported as "database", so it never counts as a new measurement.
        for name in ("ref.dng", "white.dng", "bright.dng"):
            app.upload(self.dir / name, query + "&refspec=" + quote(profile["spec"]))
        built = shared.build_payload(server._load()["photos"], profile["spec"], mons, 5)
        self.assertEqual(built[0]["reference"]["kind"], "database")

    def test_local_profile_beats_shared_and_declining_means_no_requests(self):
        app = self.new_app()
        app.json("POST", "/api/settings", json_body={"share": False})
        with mock.patch.object(shared, "_request", side_effect=AssertionError("network used")):
            for name in ("ref.dng", "white.dng", "bright.dng"):
                app.upload(self.dir / name, self.query)
            self.assertEqual(app.json("POST", "/api/share?" + self.query)[1], {"status": "off"})
            app.json("POST", "/api/reset?" + self.query)
            for p in self.other:
                status, session = app.upload(self.dir / p["name"], self.query)
        # The profile from the reference session earlier on this computer was used; no request was made.
        self.assertEqual((session["analysis"]["method"], session["analysis"]["sharedState"]), ("profile", "local"))
        self.assertEqual(session["analysis"]["profile"]["source"], "local")

    def test_unreachable_or_hostile_server(self):
        app = self.new_app()
        app.json("POST", "/api/settings", json_body={"share": True})
        for p in self.other:
            status, session = app.upload(self.dir / p["name"], self.query)
        self.assertEqual(session["analysis"]["sharedState"], "none")
        # Server down: the app carries on with the phone's own data.
        shared.forget_lookups()
        with mock.patch.dict(os.environ, {"MONCAL_API_URL": "http://127.0.0.1:9/api/v1"}):
            status, session = app.json("GET", "/api/session?" + self.query)
        self.assertEqual((status, session["analysis"]["method"], session["analysis"]["sharedState"]), (200, "camera", "error"))

    def test_answers_from_the_database_are_checked(self):
        good = {"found": True, "scope": "technology", "matrix": np.eye(3).tolist(), "contributors": 5, "spread_k": 20.0, "version": 5}
        self.assertEqual(shared.check_correction(good)["contributors"], 5)
        bad = [
            dict(good, found=False), dict(good, matrix=[[1, 0, 0], [0, 1, 0]]), dict(good, matrix=[[1, 0, 0], [0, 1, 0], [0, 0, "x"]]),
            dict(good, matrix=[[1e9, 0, 0], [0, 1, 0], [0, 0, 1]]), dict(good, matrix=[[2, 0, 0], [0, 1, 0], [0, 0, 0.1]]),
            dict(good, matrix=[[-1, 0, 0], [0, 1, 0], [0, 0, 1]]), dict(good, scope="everything"), dict(good, contributors=1),
            dict(good, contributors=True), dict(good, spread_k=1e9), dict(good, spread_k=None), [], "text", None,
            dict(good, matrix=[[float("nan"), 0, 0], [0, 1, 0], [0, 0, 1]]),
        ]
        for answer in bad:
            self.assertIsNone(shared.check_correction(answer), answer)

    def test_only_https_or_local_http(self):
        for url, ok in (("https://example.org/api/v1", True), ("http://127.0.0.1:8787/api/v1", True), ("http://localhost/api/v1", True),
                        ("http://example.org/api/v1", False), ("ftp://example.org", False), ("file:///etc/passwd", False), ("", False)):
            with mock.patch.dict(os.environ, {"MONCAL_API_URL": url}):
                self.assertEqual(shared.configured(), ok, url)

    def test_payload_has_only_allow_listed_fields(self):
        photos = [dict(p, result=dict(p["result"])) for p in self.photos]
        photos[0]["result"]["capture"] = dict(photos[0]["result"]["capture"], BodySerialNumber="SN-1", Artist="Jane", GPSLatitude=[1.0])
        payload, code, sig = shared.build_payload(photos, {"name": "srgb"}, MONITORS, 5)
        self.assertLessEqual(set(payload["capture"]), shared.privacy.CAPTURE_FIELDS)
        text = json.dumps(payload)
        for needle in ("SN-1", "Jane", "GPS", "Artist"):
            self.assertNotIn(needle, text)
        self.assertEqual(len(code), 16)
        # An unnamed screen with no model can't be filed anywhere.
        self.assertIsNone(shared.build_payload(photos, {"name": "srgb"}, {"reference": {}, "target": {}}, 5))
        # No reference shot, nothing to share.
        self.assertIsNone(shared.build_payload(photos[1:], {"name": "srgb"}, MONITORS, 5))


if __name__ == "__main__":
    unittest.main()
