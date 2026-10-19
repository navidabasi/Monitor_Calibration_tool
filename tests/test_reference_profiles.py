"""Measured reference profiles (ICC or report values per monitor model and picture mode) and the coverage counts."""

import copy
import hashlib
import secrets
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from api.v1._lib import aggregate, coverage  # noqa: E402
from monitor_calibration import privacy, shared, submission  # noqa: E402
import coverage as coverage_script  # noqa: E402
from test_api import DAY, MONITOR, Base, call, make_ctx  # noqa: E402

KEY = "monitor=DEL:41156&mode=srgb"
SRGB = [[0.64, 0.33], [0.30, 0.60], [0.15, 0.06]]
WHITE = [0.3127, 0.3290]


def spec_near(rng, shift=(0.0, 0.0), sigma=0.0008):
    """What one person's profile says: close to sRGB, with a little measurement noise."""
    jitter = lambda: float(rng.normal(0, sigma))  # noqa: E731
    return {"kind": "custom",
            "primaries": [[round(x + shift[0] + jitter(), 4), round(y + shift[1] + jitter(), 4)] for x, y in SRGB],
            "white": [round(WHITE[0] + jitter(), 4), round(WHITE[1] + jitter(), 4)]}


class Helpers(Base):
    def send(self, ctx, ip, spec, kind="icc", mode="srgb", **kw):
        charts = copy.deepcopy(self.charts)
        summary = submission.summarize(submission.analyze_shared(charts, spec))
        monitor = dict(MONITOR, screen_tech="ips_wled")
        if mode:
            monitor["picture_mode"] = mode
        (status, *_), _ = self.submit(ctx, ip=ip, charts=charts, summary=summary, reference_monitor=monitor,
                                      reference={"kind": kind, "spec": spec}, **kw)
        return status

    def fill(self, ctx, count, start=0, per_day=2, **kw):
        rng = np.random.default_rng(start + 5)
        for i in range(count):
            self.assertEqual(self.send(ctx, f"10.{start + i}.0.1", spec_near(rng), **kw), 200, i)
            if (i + 1) % per_day == 0:
                ctx.clock.t += DAY
        return ctx

    def profile(self, ctx, query=KEY):
        status, body, _ = call(ctx, "monitor_reference", method="GET", query=query, ip="203.0.113.60")
        self.assertEqual(status, 200)
        return body


class ReferenceProfiles(Helpers):
    def test_measured_profiles_are_stored_under_model_and_mode(self):
        ctx = make_ctx()
        self.assertEqual(self.send(ctx, "10.0.0.1", spec_near(np.random.default_rng(1))), 200)
        row = next(iter(ctx.store.reference_samples.values()))
        self.assertEqual((row["edid_manufacturer"], row["edid_product_code"], row["picture_mode"]), ("DEL", 41156, "srgb"))
        self.assertEqual(len(row["primaries"]), 6)
        self.assertAlmostEqual(row["primaries"][0], 0.64, delta=0.005)
        self.assertEqual(len(row["contributor_hash"]), 64)

    def test_only_real_measurements_count(self):
        ctx = make_ctx()
        rng = np.random.default_rng(2)
        self.assertEqual(self.send(ctx, "10.0.0.1", {"name": "srgb"}, kind="srgb"), 200)  # a belief, not a measurement
        self.assertEqual(self.send(ctx, "10.0.0.2", spec_near(rng), kind="database"), 200)  # somebody else's measurement
        self.assertEqual(self.send(ctx, "10.0.0.3", spec_near(rng), mode=None), 200)  # no picture mode: can't be filed
        self.assertFalse(ctx.store.reference_samples)
        self.assertEqual(self.send(ctx, "10.0.0.4", spec_near(rng), kind="manual"), 200)
        self.assertEqual(len(ctx.store.reference_samples), 1)
        # A profile that isn't physically a monitor's falls back to sRGB and is not kept.
        tiny = {"kind": "custom", "primaries": [[0.3, 0.3], [0.31, 0.3], [0.3, 0.31]], "white": WHITE}
        self.assertEqual(self.send(ctx, "10.0.0.5", tiny), 200)
        self.assertEqual(len(ctx.store.reference_samples), 1)

    def test_the_databases_own_values_never_count_as_a_new_measurement(self):
        ctx = make_ctx()
        rng = np.random.default_rng(3)
        for i in range(8):
            self.send(ctx, f"10.{i}.0.1", spec_near(rng), kind="database")
            ctx.clock.t += DAY
        self.assertEqual(self.profile(ctx), {"found": False})

    def test_nothing_is_shown_without_enough_independent_people(self):
        ctx = make_ctx()
        self.fill(ctx, 4)
        self.assertEqual(self.profile(ctx), {"found": False})  # no count revealed either
        self.fill(ctx, 1, start=20, per_day=1)
        got = self.profile(ctx)
        self.assertTrue(got["found"])
        self.assertEqual(got["contributors"], 5)
        for (x, y), (wx, wy) in zip(got["primaries"], SRGB):
            self.assertAlmostEqual(x, wx, delta=0.003)
            self.assertAlmostEqual(y, wy, delta=0.003)
        self.assertAlmostEqual(got["white"][0], WHITE[0], delta=0.003)
        self.assertLessEqual(got["spread"], 0.01)

    def test_needs_two_days_and_one_voice_per_network(self):
        ctx = make_ctx()
        self.fill(ctx, 6, per_day=100)
        self.assertEqual(self.profile(ctx), {"found": False})
        ctx = make_ctx()
        rng = np.random.default_rng(4)
        for i in range(8):
            self.send(ctx, f"10.9.0.{i + 1}", spec_near(rng))  # one household
            ctx.clock.t += DAY
        self.assertEqual(self.profile(ctx), {"found": False})

    def test_disagreeing_profiles_are_not_for_the_same_screen(self):
        ctx = make_ctx()
        rng = np.random.default_rng(5)
        for i, shift in enumerate([(0, 0), (0.03, 0), (-0.03, 0.02), (0, 0.04), (0.05, -0.03), (-0.04, -0.04)]):
            self.send(ctx, f"10.{i}.0.1", spec_near(rng, shift=shift))
            ctx.clock.t += DAY
        self.assertEqual(self.profile(ctx), {"found": False})

    def test_other_modes_and_models_are_other_cases(self):
        ctx = make_ctx()
        self.fill(ctx, 6)
        self.assertTrue(self.profile(ctx)["found"])
        self.assertEqual(self.profile(ctx, "monitor=DEL:41156&mode=game"), {"found": False})
        self.assertEqual(self.profile(ctx, "monitor=DEL:41157&mode=srgb"), {"found": False})

    def test_deleting_by_code_removes_the_profile_sample(self):
        ctx = make_ctx()
        self.fill(ctx, 5)
        code = secrets.token_bytes(16)
        self.send(ctx, "10.88.0.1", spec_near(np.random.default_rng(6)), deletion_hash=hashlib.sha256(code).hexdigest())
        self.assertEqual(len(ctx.store.reference_samples), 6)
        call(ctx, "submissions_delete", body={"code": code.hex()}, ip="203.0.113.77")
        self.assertEqual(len(ctx.store.reference_samples), 5)

    def test_query_validation(self):
        ctx = make_ctx()
        for q in ("", "monitor=DEL:1", "monitor=x&mode=srgb", "monitor=DEL:1&mode=nope", "monitor=DEL:1&mode=srgb&x=1"):
            self.assertEqual(call(ctx, "monitor_reference", method="GET", query=q)[0], 400, q)

    def test_hashes_expire_but_samples_stay(self):
        ctx = make_ctx()
        self.fill(ctx, 6)
        ctx.clock.t += 61 * DAY
        ctx.store.purge(ctx.now(), aggregate.hashing.day_of(ctx.now()), "")
        self.assertTrue(all(s["contributor_hash"] is None for s in ctx.store.reference_samples.values()))
        self.assertTrue(self.profile(ctx)["found"])


class Coverage(Helpers):
    def test_counts_only(self):
        ctx = make_ctx()
        self.assertEqual(coverage.build(ctx.store), {
            "phones_verified": 0, "monitors_verified": 0,
            "phone_corrections": {"phones_covered": 0, "by_screen_tech": {}},
            "suggested_settings": {"monitor_modes": 0}, "reference_profiles": {"monitor_modes": 0}})
        self.fill(ctx, 6)
        aggregate.run(ctx)
        status, body, headers = call(ctx, "coverage", method="GET", ip="203.0.113.61")
        self.assertEqual(status, 200)
        self.assertEqual(body["phones_verified"], 1)
        self.assertEqual(body["reference_profiles"]["monitor_modes"], 1)
        self.assertEqual(body["phone_corrections"], {"phones_covered": 1, "by_screen_tech": {"ips_wled": 1}})
        self.assertEqual(headers["Cache-Control"], "public, max-age=3600")
        text = repr(body)
        for needle in ("DELL", "U2722", "iPhone", "DEL:", "10.0"):
            self.assertNotIn(needle, text)  # totals only: no model names
        self.assertEqual(call(ctx, "coverage", method="POST")[0], 405)

    def test_the_page_is_rendered_from_the_counts(self):
        ctx = make_ctx()
        self.fill(ctx, 6)
        aggregate.run(ctx)
        md = coverage_script.render(coverage.build(ctx.store), when="2026-10-19")
        self.assertIn("as of 2026-10-19", md)
        self.assertIn("| Monitor models and picture modes with a measured reference profile | 1 |", md)
        self.assertIn("IPS (white LED)", md)
        self.assertNotIn("DELL", md)


class Client(unittest.TestCase):
    GOOD = {"found": True, "primaries": SRGB, "white": WHITE, "contributors": 7, "spread": 0.002}

    def test_good_answer_becomes_a_spec(self):
        got = shared.check_reference(self.GOOD)
        self.assertEqual(got["spec"]["source"], "shared")
        self.assertEqual(got["spec"]["primaries"], SRGB)
        self.assertEqual(privacy.reference_kind(got["spec"]), "database")

    def test_hostile_answers_are_ignored(self):
        bad = [dict(self.GOOD, found=False), dict(self.GOOD, contributors=4), dict(self.GOOD, contributors=True),
               dict(self.GOOD, spread=0.5), dict(self.GOOD, primaries=SRGB[:2]), dict(self.GOOD, white=[0.3]),
               dict(self.GOOD, primaries=[[5, 5], [0.3, 0.6], [0.15, 0.06]]), dict(self.GOOD, white=[float("nan"), 0.3]),
               dict(self.GOOD, primaries=[[0.3, 0.3], [0.31, 0.3], [0.3, 0.31]]),  # not a monitor
               dict(self.GOOD, primaries=[["a", "b"]] * 3), [], "x", None]
        for answer in bad:
            self.assertIsNone(shared.check_reference(answer), answer)

    def test_reference_kinds(self):
        self.assertEqual(privacy.reference_kind({"kind": "custom", "source": "icc"}), "icc")
        self.assertEqual(privacy.reference_kind({"kind": "custom", "source": "manual"}), "manual")
        self.assertEqual(privacy.reference_kind({"kind": "custom", "source": "shared"}), "database")
        self.assertEqual(privacy.reference_kind({"kind": "preset", "name": "p3"}), "p3")


if __name__ == "__main__":
    unittest.main()
