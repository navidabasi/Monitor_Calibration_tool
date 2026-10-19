"""Phase 3: the hosted API, run in-process against an in-memory database.

Covers bad schemas, oversize bodies, bad or reused proof of work, rate limits,
a fabricated phone matrix, a hand-edited summary, poisoning of the shared
correction, deletion by code, and that raw IP addresses never reach storage.
"""

import contextlib
import copy
import hashlib
import io
import json
import secrets
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from api.v1._lib import aggregate, proof, service  # noqa: E402
from api.v1._lib.config import Config, Context  # noqa: E402
from api.v1._lib.store import MemoryStore  # noqa: E402
from monitor_calibration import analyze, submission  # noqa: E402
from monitor_calibration import colorimetry as cm  # noqa: E402
from monitor_calibration.server import process_file  # noqa: E402
import synth  # noqa: E402

EMBEDDED = synth.CAMERA @ np.array([[1.02, 0.0, -0.03], [0.0, 1.0, 0.0], [-0.02, 0.0, 1.04]])
PHONE = dict(make="Apple", model="iPhone Test", unique="iPhone Test back camera", software="Adobe Photoshop Lightroom 9.0",
             lens="iPhone Test back camera 6.765mm f/1.78")
DAY = 86400
T0 = 1_792_000_000  # a fixed "now" (October 2026)
SITE = "https://calibrate.example"
MONITOR = {"manufacturer": "DEL", "product_code": 41156, "model_name": "DELL U2722DE", "year": 2022, "screen_tech": "ips_wled"}
TARGET_MONITOR = {"manufacturer": "AUS", "product_code": 43537, "model_name": "XG27AQDMES", "screen_tech": "woled", "picture_mode": "srgb"}


class FakeClock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def make_ctx(**cfg):
    clock = FakeClock()
    config = Config(pow_secret=b"p" * 32, ip_secret=b"i" * 32, site_origin=SITE, cron_secret="cron-secret-value",
                    turnstile_secret="ts-secret", pow_difficulty=8, **cfg)
    ctx = Context(config=config, store=MemoryStore(), clock=clock)
    ctx.sent = []
    ctx.http_post = lambda url, data: (ctx.sent.append(data), {"success": data["response"] == "good-token"})[1]
    return ctx


def call(ctx, route, method="POST", body=None, query="", ip="203.0.113.5", headers=None, raw=None):
    h = {"x-real-ip": ip, "content-type": "application/json"}
    h.update(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    return service.handle(ctx, route, method, query, h, data)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.n = 0
        cls.photos = [cls.shot("ref.dng", synth.reference_display(), "bright", "reference"),
                      cls.shot("white.dng", synth.Display(synth.P3_TO_XYZ, gains=(0.97, 1, 1.06), gamma=2.35, luminance=1.2), "white", "target"),
                      cls.shot("bright.dng", synth.Display(synth.P3_TO_XYZ, gains=(0.97, 1, 1.06), gamma=2.35, luminance=1.2), "bright", "target")]
        cls.charts, cls.summary, cls.analysis = submission.build(cls.photos, {"name": "srgb"})
        cls.capture = cls.photos[0]["result"]["capture"]
        cls.true_k = np.array(cls.analysis["correction"]["k"])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def shot(cls, name, display, typ, role):
        cls.n += 1
        path = cls.dir / name
        synth.write_dng(path, synth.render(display, typ, role, exposure=0.32, seed=cls.n), 1 / 60, iso=50,
                        color_matrix=EMBEDDED, **PHONE)
        result, error = process_file(path)
        assert error is None, error
        return {"id": cls.n, "name": name, "result": result}

    # --- building requests
    def payload(self, ctx, ip="203.0.113.5", charts=None, summary=None, tech="ips_wled", capture=None, hosted=False, **over):
        charts = charts if charts is not None else copy.deepcopy(self.charts)
        if summary is None:
            summary = submission.summarize(submission.analyze_shared(charts, {"name": "srgb"}))
        ch = call(ctx, "challenge", ip=ip)[1]
        ch = dict(ch, counter=proof.solve(ch))
        body = {
            "v": 1, "challenge": ch, "deletion_hash": hashlib.sha256(secrets.token_bytes(16)).hexdigest(),
            "app_version": "0.1.0", "analysis_version": 5, "session_month": "2026-10",
            "capture": capture or self.capture,
            "reference_monitor": dict(MONITOR, screen_tech=tech),
            "target_monitor": TARGET_MONITOR,
            "reference": {"kind": "srgb", "spec": {"name": "srgb"}},
            "charts": charts, "summary": summary,
        }
        body.update(over)
        return body

    def submit(self, ctx, ip="203.0.113.5", headers=None, **kw):
        body = self.payload(ctx, ip=ip, **kw)
        return call(ctx, "submissions", body=body, ip=ip, headers=headers), body

    def jitter(self, rng, sigma=0.002, scale=(1, 1, 1)):
        """Another person's session: the same phone and kind of screen, a little measurement noise."""
        charts = copy.deepcopy(self.charts)
        for c in charts:
            for p in c["patches"]:
                factor = np.array(scale) if (c["type"], c["role"]) == ("bright", "reference") else 1.0
                p["signal"] = (np.array(p["signal"]) * (1 + rng.normal(0, sigma, 3)) * factor).tolist()
        return charts


class Submissions(Base):
    def test_valid_submission_is_stored_pending(self):
        ctx = make_ctx()
        (status, body, headers), _ = self.submit(ctx)
        self.assertEqual((status, body), (200, {"status": "received"}))
        self.assertEqual(headers["Cache-Control"], "no-store")
        sub = next(iter(ctx.store.submissions.values()))
        self.assertEqual(sub["status"], "pending")
        self.assertEqual(sub["received_on"], service.hashing.day_of(T0))
        self.assertEqual(sub["session_month"], "2026-10-01")
        self.assertEqual(sub["reference_screen_tech"], "ips_wled")
        self.assertTrue(sub["server_result"]["checks"]["passed"])
        self.assertEqual(len(ctx.store.phones), 1)
        self.assertEqual(len(ctx.store.monitors), 2)

    def test_nothing_identifying_is_stored(self):
        ctx = make_ctx()
        self.submit(ctx, ip="198.51.100.77")
        text = json.dumps(ctx.store.submissions, default=str) + json.dumps(ctx.store.phones) + repr(ctx.store._rate) + repr(ctx.store._nonces)
        for needle in ("198.51.100.77", "198.51.100", ".dng", "SN-", "Jane"):
            self.assertNotIn(needle, text)
        sub = next(iter(ctx.store.submissions.values()))
        self.assertEqual(len(sub["contributor_hash"]), 64)
        self.assertNotIn("timestamp", json.dumps(sub))
        # No raw dates or times: only a month on the session and a day on receipt.
        self.assertEqual(sub["session_month"][8:], "01")

    def test_schema_violations_are_rejected_generically(self):
        ctx = make_ctx()
        good = self.payload(ctx)
        cases = {
            "unknown field": dict(good, owner="Jane"),
            "unknown capture field": dict(good, capture=dict(self.capture, BodySerialNumber="SN-1")),
            "gps in capture": dict(good, capture=dict(self.capture, GPSLatitude=[1.0])),
            "bad enum": dict(good, reference_monitor=dict(MONITOR, screen_tech="plasma")),
            "free text picture mode": dict(good, target_monitor=dict(TARGET_MONITOR, picture_mode="Jane's mode")),
            "missing field": {k: v for k, v in good.items() if k != "summary"},
            "wrong type": dict(good, analysis_version="5"),
            "bool as number": dict(good, analysis_version=True),
            "exact date": dict(good, session_month="2026-10-18"),
            "string too long": dict(good, app_version="1" * 50),
            "too many charts": dict(good, charts=good["charts"] * 2),
            "no reference chart": dict(good, charts=[c for c in good["charts"] if c["role"] != "reference"]),
            "huge number": dict(good, summary=dict(good["summary"], ref_gamma=1e30)),
        }
        for i, (name, body) in enumerate(cases.items()):
            status, answer, _ = call(ctx, "submissions", body=body, ip=f"192.0.2.{i + 1}")
            self.assertEqual(status, 400, name)
            self.assertEqual(answer, {"error": "bad_request"}, name)
        self.assertFalse(ctx.store.submissions)

    def test_nan_infinity_and_junk_json(self):
        ctx = make_ctx()
        text = json.dumps(self.payload(ctx)).replace('"ref_gamma"', '"ref_gamma_x"')
        for raw in (b"not json", b"[]", b'{"a": NaN}', b'{"a": Infinity}', b"\xff\xfe", text.encode()):
            self.assertEqual(call(ctx, "submissions", raw=raw)[0], 400, raw[:20])
        deep = b"[" * 5000 + b"]" * 5000
        self.assertEqual(call(ctx, "submissions", raw=deep)[0], 400)

    def test_oversize_body_and_wrong_content_type(self):
        ctx = make_ctx()
        self.assertEqual(call(ctx, "submissions", raw=b"{" + b" " * 70_000 + b"}")[0], 413)
        self.assertEqual(call(ctx, "submissions", headers={"content-type": "text/plain"}, raw=b"{}")[0], 415)
        self.assertEqual(call(ctx, "submissions", method="GET")[0], 405)
        self.assertEqual(call(ctx, "nothing")[0], 404)

    def test_proof_of_work(self):
        ctx = make_ctx()
        body = self.payload(ctx)
        # A counter that doesn't solve it.
        bad = dict(body, challenge=dict(body["challenge"], counter=body["challenge"]["counter"] + 1))
        while proof.work_ok(bad["challenge"]["nonce"], bad["challenge"]["counter"], 8):
            bad["challenge"]["counter"] += 1
        self.assertEqual(call(ctx, "submissions", body=bad)[0], 403)
        # An edited challenge (easier difficulty) breaks the signature.
        easy = dict(body, challenge=dict(body["challenge"], difficulty=1))
        self.assertEqual(call(ctx, "submissions", body=easy)[0], 403)
        # Made-up nonce.
        fake = dict(body, challenge=dict(body["challenge"], nonce="0" * 32))
        self.assertEqual(call(ctx, "submissions", body=fake)[0], 403)
        # The real one works once and only once.
        self.assertEqual(call(ctx, "submissions", body=body)[0], 200)
        self.assertEqual(call(ctx, "submissions", body=body)[0], 403)
        # Expired.
        old = self.payload(ctx)
        ctx.clock.t += 3600
        self.assertEqual(call(ctx, "submissions", body=old)[0], 403)

    def test_rate_limits(self):
        ctx = make_ctx()
        results = [call(ctx, "challenge", ip="192.0.2.9")[0] for _ in range(32)]
        self.assertEqual(results.count(200), 30)
        self.assertEqual(results[-1], 429)
        # Another address is unaffected; the same address recovers in the next window.
        self.assertEqual(call(ctx, "challenge", ip="192.0.2.10")[0], 200)
        ctx.clock.t += 3600
        self.assertEqual(call(ctx, "challenge", ip="192.0.2.9")[0], 200)
        # Submissions: 10 a day, counting failed attempts too.
        ctx = make_ctx()
        codes = [call(ctx, "submissions", body={"junk": i}, ip="192.0.2.50")[0] for i in range(12)]
        self.assertEqual(codes, [400] * 10 + [429] * 2)

    def test_hand_edited_summary_is_rejected(self):
        ctx = make_ctx()
        honest = submission.summarize(submission.analyze_shared(self.charts, {"name": "srgb"}))
        for name, edit in {
            "matrix": lambda s: s.update(a_ref=[v * 1.05 for v in s["a_ref"]]),
            "gamma": lambda s: s.update(ref_gamma=2.2 if abs(s["ref_gamma"] - 2.2) > 0.05 else 2.5),
            "white point": lambda s: s.update(round_cct=s["round_cct"] + 400),
            "tint": lambda s: s.update(ref_max_tint=s["ref_max_tint"] + 3),
        }.items():
            summary = copy.deepcopy(honest)
            edit(summary)
            (status, *_), _ = self.submit(ctx, summary=summary, ip=f"192.0.2.{len(name)}")
            self.assertEqual(status, 422, name)
        # Edited readings with a summary that still describes the originals: also caught.
        charts = copy.deepcopy(self.charts)
        for p in charts[0]["patches"]:
            p["signal"][2] *= 1.1
        (status, *_), _ = self.submit(ctx, charts=charts, summary=honest, ip="192.0.2.99")
        self.assertEqual(status, 422)
        self.assertFalse(ctx.store.submissions)

    def test_implausible_sessions_are_rejected(self):
        ctx = make_ctx()
        cases = {}
        c = copy.deepcopy(self.charts)
        c[0]["framing"]["fraction"] = 0.7
        cases["chart too big"] = dict(charts=c)
        c = copy.deepcopy(self.charts)
        c[0]["patches"][7]["clipped"] = True
        cases["clipped"] = dict(charts=c)
        c = copy.deepcopy(self.charts)
        c[0]["exposure"]["norm"] *= 2
        cases["exposure inconsistent"] = dict(charts=c)
        c = copy.deepcopy(self.charts)
        for ch in c:  # a blue channel three times too strong: a different white than the reference shows
            ch["cam_to_xyz"] = [v * 3 if i % 3 == 2 else v for i, v in enumerate(ch["cam_to_xyz"])]
        cases["maker matrix absurd"] = dict(charts=c)
        for i, (name, kw) in enumerate(cases.items()):
            (status, *_), _ = self.submit(ctx, ip=f"192.0.2.{i + 1}", **kw)
            self.assertEqual(status, 422, name)
        # A tag that disagrees with the exposure the readings were normalized with.
        (status, *_), _ = self.submit(ctx, capture=dict(self.capture, ExposureTime=1 / 8), ip="192.0.2.77")
        self.assertEqual(status, 422)
        # A correction can't be filed under "unknown" without a monitor model to name it.
        (status, *_), _ = self.submit(ctx, tech="unknown", reference_monitor={"screen_tech": "unknown"}, ip="192.0.2.78")
        self.assertEqual(status, 422)
        self.assertFalse(ctx.store.submissions)

    def test_fabricated_phone_matrix(self):
        ctx = make_ctx()
        rng = np.random.default_rng(1)
        for i in range(3):
            self.submit(ctx, ip=f"10.0.{i}.1", charts=self.jitter(rng))
            ctx.clock.t += DAY
        aggregate.run(ctx)
        phone = next(iter(ctx.store.phones.values()))
        self.assertEqual(phone["status"], "verified")
        self.assertAlmostEqual(phone["embedded_color_matrix"][0], self.capture["ColorMatrix1"][0], places=4)
        # Same phone name, different color data: refused.
        fake = dict(self.capture, ColorMatrix1=[v * 1.1 for v in self.capture["ColorMatrix1"]])
        (status, *_), _ = self.submit(ctx, capture=fake, ip="10.9.9.9")
        self.assertEqual(status, 422)
        (status, *_), _ = self.submit(ctx, ip="10.9.9.10")
        self.assertEqual(status, 200)

    def test_turnstile_for_the_hosted_site(self):
        ctx = make_ctx()
        site = {"origin": SITE}
        (status, *_), _ = self.submit(ctx, ip="198.51.100.1", headers=site)
        self.assertEqual(status, 403)  # no token
        (status, *_), _ = self.submit(ctx, ip="198.51.100.2", headers=site, turnstile="bad-token")
        self.assertEqual(status, 403)
        (status, _, headers), _ = self.submit(ctx, ip="198.51.100.3", headers=site, turnstile="good-token")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], SITE)
        self.assertNotIn("remoteip", ctx.sent[0])  # the visitor's address isn't passed on
        # The local app has no Origin and no Turnstile; it still pays proof of work.
        (status, *_), _ = self.submit(ctx, ip="198.51.100.4")
        self.assertEqual(status, 200)

    def test_origin_must_be_the_hosted_site(self):
        ctx = make_ctx()
        self.assertEqual(call(ctx, "challenge", headers={"origin": "https://evil.example"})[0], 403)
        status, _, headers = call(ctx, "challenge", method="OPTIONS", headers={"origin": SITE})
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], SITE)
        self.assertEqual(call(ctx, "challenge", method="OPTIONS", headers={"origin": "https://evil.example"})[0], 403)
        self.assertEqual(call(ctx, "challenge", headers={"origin": SITE})[0], 200)

    def test_errors_never_leak_details(self):
        ctx = make_ctx()
        ctx.store.upsert_phone = lambda key: 1 / 0
        with contextlib.redirect_stderr(io.StringIO()):  # the traceback belongs in the server log
            (status, body, _), _ = self.submit(ctx)
        self.assertEqual((status, body), (500, {"error": "server_error"}))

    def test_cron_needs_its_secret(self):
        ctx = make_ctx()
        self.assertEqual(call(ctx, "cron_aggregate", method="GET")[0], 403)
        self.assertEqual(call(ctx, "cron_aggregate", method="GET", headers={"authorization": "Bearer nope"})[0], 403)
        status, body, _ = call(ctx, "cron_aggregate", method="GET", headers={"authorization": "Bearer cron-secret-value"})
        self.assertEqual((status, body["status"]), (200, "ok"))


class Helpers(Base):
    """Shared by the aggregation tests and the review tool tests."""

    def fill(self, ctx, count, rng, start_ip=0, scale=(1, 1, 1), days=3, tech="ips_wled"):
        for i in range(count):
            (status, *_), body = self.submit(ctx, ip=f"10.{start_ip + i}.0.1", charts=self.jitter(rng, scale=scale), tech=tech)
            self.assertEqual(status, 200, i)
            ctx.clock.t += DAY // days if days else 0

    def phone_key(self, ctx):
        phone = next(iter(ctx.store.phones.values()))
        return {"unique_camera_model": phone["unique_camera_model"], "lens_model": phone["lens_model"],
                "raw_type": phone["raw_type"], "app_family": phone["app_family"]}

    def lookup(self, ctx, tech="ips_wled", monitor=None):
        q = "&".join(f"{k}={v.replace(' ', '%20')}" for k, v in self.phone_key(ctx).items())
        q += f"&tech={tech}" + (f"&monitor={monitor}" if monitor else "")
        return call(ctx, "corrections", method="GET", query=q, ip="203.0.113.99")[1]


class Aggregation(Helpers):
    """The shared correction: who is believed, and who isn't."""

    def test_correction_needs_three_independent_contributors(self):
        ctx = make_ctx()
        rng = np.random.default_rng(2)
        self.fill(ctx, 2, rng)
        aggregate.run(ctx)
        self.assertEqual(self.lookup(ctx), {"found": False})
        self.fill(ctx, 1, rng, start_ip=5)
        counts = aggregate.run(ctx)
        self.assertEqual(counts["accepted"], 3)
        found = self.lookup(ctx)
        self.assertTrue(found["found"])
        self.assertEqual((found["contributors"], found["scope"]), (3, "technology"))
        self.assertLess(np.abs(np.array(found["matrix"]) - self.true_k).max(), 0.01)
        # Three contributors also verified the monitor model, so it has its own entry; other models fall back to
        # the screen technology, and other kinds of screen get nothing.
        self.assertEqual(self.lookup(ctx, monitor="DEL:41156")["scope"], "monitor")
        self.assertEqual(self.lookup(ctx, monitor="DEL:1")["scope"], "technology")
        self.assertEqual(self.lookup(ctx, tech="woled"), {"found": False})

    def test_same_network_counts_once(self):
        ctx = make_ctx()
        rng = np.random.default_rng(3)
        for _ in range(6):  # one household, six sessions, on different days
            self.submit(ctx, ip="10.1.1.1", charts=self.jitter(rng))
            ctx.clock.t += DAY
        for ip in ("10.1.1.200", "10.1.1.9"):  # same /24
            self.submit(ctx, ip=ip, charts=self.jitter(rng))
        aggregate.run(ctx)
        self.assertEqual(self.lookup(ctx), {"found": False})
        self.assertEqual(sum(1 for s in ctx.store.submissions.values() if s["status"] == "accepted"), 0)

    def test_needs_different_days(self):
        ctx = make_ctx()
        rng = np.random.default_rng(4)
        self.fill(ctx, 4, rng, days=0)  # all on the same day
        aggregate.run(ctx)
        self.assertEqual(self.lookup(ctx), {"found": False})
        ctx.clock.t += DAY
        self.fill(ctx, 1, rng, start_ip=20)
        aggregate.run(ctx)
        self.assertTrue(self.lookup(ctx)["found"])

    def test_poisoning_leaves_the_median_alone(self):
        ctx = make_ctx()
        rng = np.random.default_rng(5)
        honest_ccts = []
        # Outliers carry a different color cast on the reference shot: plausible enough to be stored, not to be believed.
        self.fill(ctx, 10, rng, start_ip=0)
        self.fill(ctx, 5, rng, start_ip=100, scale=(0.9, 1.0, 1.12))
        stored = list(ctx.store.submissions.values())
        self.assertEqual(len(stored), 15)
        outlier_ids = {s["id"] for s in stored[10:]}
        k_out = np.array(stored[12]["server_result"]["k"])
        print(f"\noutlier shifts the white by {abs(analyze.white_after_correction(k_out)[0] - analyze.white_after_correction(self.true_k)[0]):.0f} K")
        counts = aggregate.run(ctx)
        accepted = {s["id"] for s in ctx.store.submissions.values() if s["status"] == "accepted"}
        self.assertFalse(accepted & outlier_ids)
        self.assertEqual(counts["accepted"], 10)
        found = self.lookup(ctx)
        truth = analyze.white_after_correction(self.true_k)[0]
        got = analyze.white_after_correction(np.array(found["matrix"]))[0]
        print(f"median white {got:.0f} K vs truth {truth:.0f} K, spread {found['spread_k']} K")
        self.assertLess(abs(got - truth), 30)
        self.assertEqual(found["contributors"], 10)
        # More outliers later (the correction exists now) are still turned away.
        ctx.clock.t += DAY
        self.fill(ctx, 3, rng, start_ip=150, scale=(0.9, 1.0, 1.12))
        aggregate.run(ctx)
        self.assertEqual(self.lookup(ctx)["contributors"], 10)
        self.assertGreaterEqual(sum(1 for s in ctx.store.submissions.values() if s["status"] == "quarantined"), 3)

    def test_outliers_alone_cannot_start_a_key_when_they_disagree(self):
        ctx = make_ctx()
        rng = np.random.default_rng(6)
        for i, scale in enumerate([(0.8, 1, 1.0), (1.0, 1, 1.15), (0.9, 1, 1.12), (1.1, 1, 0.85)]):
            self.submit(ctx, ip=f"10.{i}.0.1", charts=self.jitter(rng, scale=scale))
            ctx.clock.t += DAY
        aggregate.run(ctx)
        self.assertEqual(self.lookup(ctx), {"found": False})

    def test_slow_drift_is_followed_but_a_jump_freezes_the_key(self):
        def white(m):
            return analyze.white_after_correction(np.array(m))[0]

        # Newcomers who read about 110 K cooler: believable, and the correction follows (under the 150 K tripwire).
        ctx = make_ctx()
        rng = np.random.default_rng(7)
        self.fill(ctx, 4, rng)
        aggregate.run(ctx)
        before = self.lookup(ctx)["matrix"]
        self.fill(ctx, 20, rng, start_ip=30, scale=(1.0, 1.0, 1.02), days=1)
        self.assertEqual(aggregate.run(ctx)["frozen"], 0)
        followed = self.lookup(ctx)
        self.assertEqual(followed["contributors"], 24)
        self.assertLess(abs(white(followed["matrix"]) - white(before)), aggregate.FREEZE_SHIFT)

        # Newcomers 170 K away: each is believable, but together they would move the answer by more than 150 K.
        ctx = make_ctx()
        rng = np.random.default_rng(7)
        self.fill(ctx, 4, rng)
        aggregate.run(ctx)
        self.fill(ctx, 20, rng, start_ip=30, scale=(1.0, 1.0, 1.03), days=1)
        counts = aggregate.run(ctx)
        self.assertGreater(counts["frozen"], 0)
        frozen = ctx.store.list_corrections(frozen=True)
        self.assertTrue(frozen)
        self.assertEqual(self.lookup(ctx), {"found": False})  # nothing is served while it waits for review
        self.assertTrue(all(c["contributors"] == 4 for c in frozen))  # the old value is kept, not replaced

    def test_flood_freezes_the_key(self):
        ctx = make_ctx()
        rng = np.random.default_rng(8)
        self.fill(ctx, 22, rng, days=0)  # 22 pending in one day
        counts = aggregate.run(ctx)
        self.assertEqual(counts["frozen"], 1)
        self.assertEqual(self.lookup(ctx), {"found": False})
        self.assertTrue(ctx.store.list_corrections(frozen=True))
        # While frozen nothing more is accepted, even later.
        ctx.clock.t += DAY
        self.fill(ctx, 3, rng, start_ip=60)
        aggregate.run(ctx)
        self.assertEqual(sum(1 for s in ctx.store.submissions.values() if s["status"] == "accepted"), 0)

    def test_deleting_by_code(self):
        ctx = make_ctx()
        rng = np.random.default_rng(9)
        codes = []
        for i in range(4):
            code = secrets.token_bytes(16)
            codes.append(code)
            (status, *_), _ = self.submit(ctx, ip=f"10.{i}.0.1", charts=self.jitter(rng), deletion_hash=hashlib.sha256(code).hexdigest())
            self.assertEqual(status, 200)
            ctx.clock.t += DAY
        aggregate.run(ctx)
        self.assertEqual(self.lookup(ctx)["contributors"], 4)
        status, body, _ = call(ctx, "submissions_delete", body={"code": codes[0].hex()}, ip="203.0.113.77")
        self.assertEqual((status, body), (200, {"status": "ok"}))
        self.assertEqual(self.lookup(ctx)["contributors"], 3)
        self.assertEqual(len(ctx.store.submissions), 3)
        # Below three contributors the correction is withdrawn.
        call(ctx, "submissions_delete", body={"code": codes[1].hex()}, ip="203.0.113.77")
        self.assertEqual(self.lookup(ctx), {"found": False})
        # Same answer for a code that matches nothing, and for malformed ones.
        self.assertEqual(call(ctx, "submissions_delete", body={"code": secrets.token_hex(16)}, ip="203.0.113.77")[1], {"status": "ok"})
        self.assertEqual(call(ctx, "submissions_delete", body={"code": "zz"}, ip="203.0.113.77")[0], 400)
        self.assertEqual(call(ctx, "submissions_delete", body={"code": "a" * 32, "extra": 1}, ip="203.0.113.77")[0], 400)

    def test_deletion_survives_a_frozen_key(self):
        ctx = make_ctx()
        rng = np.random.default_rng(10)
        code = secrets.token_bytes(16)
        self.submit(ctx, ip="10.0.0.1", charts=self.jitter(rng), deletion_hash=hashlib.sha256(code).hexdigest())
        ctx.clock.t += DAY
        self.fill(ctx, 3, rng, start_ip=5)
        aggregate.run(ctx)
        call(ctx, "submissions_delete", body={"code": code.hex()}, ip="203.0.113.77")
        self.assertEqual(len(ctx.store.submissions), 3)

    def test_lookup_validation_and_limits(self):
        ctx = make_ctx()
        bad = ["", "unique_camera_model=x", "unique_camera_model=x&lens_model=&raw_type=jpeg&app_family=other",
               "unique_camera_model=x&lens_model=&raw_type=bayer&app_family=other&extra=1",
               "unique_camera_model=x&unique_camera_model=y&lens_model=&raw_type=bayer&app_family=other",
               "unique_camera_model=x&lens_model=&raw_type=bayer&app_family=other&monitor=bad"]
        for q in bad:
            self.assertEqual(call(ctx, "corrections", method="GET", query=q)[0], 400, q)
        ok = "unique_camera_model=x&lens_model=&raw_type=bayer&app_family=other&tech=woled"
        self.assertEqual(call(ctx, "corrections", method="GET", query=ok)[1], {"found": False})
        codes = [call(ctx, "corrections", method="GET", query=ok, ip="192.0.2.20")[0] for _ in range(62)]
        self.assertEqual(codes[-1], 429)

    def test_monitor_search_only_shows_verified(self):
        ctx = make_ctx()
        rng = np.random.default_rng(11)
        self.fill(ctx, 3, rng)
        self.assertEqual(call(ctx, "monitors_search", method="GET", query="q=DELL")[1], {"monitors": []})  # still pending
        aggregate.run(ctx)
        found = call(ctx, "monitors_search", method="GET", query="q=dell")[1]["monitors"]
        self.assertEqual(found, [{"name": "DELL U2722DE", "edid": "DEL:41156", "screen_tech": "ips_wled"}])
        # The target monitor of the same sessions is verified too.
        self.assertEqual(call(ctx, "monitors_search", method="GET", query="q=XG27")[1]["monitors"][0]["edid"], "AUS:43537")
        self.assertEqual(call(ctx, "monitors_search", method="GET", query="q=de")[0], 400)  # too short
        self.assertEqual(call(ctx, "monitors_search", method="GET", query="q=" + "a" * 50)[0], 400)

    def test_exact_monitor_beats_technology(self):
        ctx = make_ctx()
        rng = np.random.default_rng(12)
        self.fill(ctx, 3, rng)
        aggregate.run(ctx)
        found = self.lookup(ctx, monitor="DEL:41156")
        self.assertEqual(found["scope"], "monitor")
        self.assertEqual(self.lookup(ctx, monitor="DEL:1")["scope"], "technology")

    def test_scheduled_cleanup(self):
        ctx = make_ctx()
        rng = np.random.default_rng(13)
        self.fill(ctx, 3, rng)
        aggregate.run(ctx)
        ctx.clock.t += 61 * DAY
        today = service.hashing.day_of(ctx.now())
        ctx.store.purge(ctx.now(), today, service.hashing.month_of(ctx.now()))
        subs = ctx.store.submissions.values()
        self.assertTrue(all(s["contributor_hash"] is None for s in subs))
        self.assertEqual(len(ctx.store._rate), 0)
        self.assertEqual(len(ctx.store._nonces), 0)
        self.assertEqual({k for k in ctx.store._salts if k[0] == "day"}, set())
        # The accepted contributions still count after their network hashes are gone.
        self.assertEqual(self.lookup(ctx)["contributors"], 3)


if __name__ == "__main__":
    unittest.main()
