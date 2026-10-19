"""Shared monitor settings: what is collected, and when a suggestion may be shown.

A suggestion needs at least 5 independent contributors on at least 2 days, who said they calibrated the same
monitor model in the same picture mode, whose measurements (recomputed by the server) reached 2 delta E or
less, and whose values agree. Anything less shows nothing.
"""

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

from api.v1._lib import recipes  # noqa: E402
from monitor_calibration import privacy, shared, submission  # noqa: E402
import synth  # noqa: E402
from test_api import DAY, TARGET_MONITOR, Base, call, make_ctx  # noqa: E402

KEY = "monitor=AUS:43537&mode=srgb"


class Recipes(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A bright chart of a monitor that is already on target (D65), and one that isn't (the original, ~7000 K).
        good = cls.shot("bright_good.dng", synth.reference_display(), "bright", "target")
        photos = [cls.photos[0], cls.photos[1], good]
        cls.good_charts = submission.build(photos, {"name": "srgb"}, need_round=True)[0]
        cls.off_charts = copy.deepcopy(cls.charts)

    def sample(self, ctx, ip, calibrated=True, rgb=(97, 100, 94), mx=100, good=True, **kw):
        charts = copy.deepcopy(self.good_charts if good else self.off_charts)
        settings = {"calibrated": calibrated, "rgb": {"r": rgb[0], "g": rgb[1], "b": rgb[2], "max": mx}}
        (status, *_), _ = self.submit(ctx, ip=ip, charts=charts, settings=settings, **kw)
        return status

    def fill(self, ctx, count, start=0, per_day=2, rgb=(97, 100, 94), **kw):
        """`count` different people (one network each), `per_day` of them per day, each within a point of `rgb`."""
        rng = np.random.default_rng(start + 1)
        for i in range(count):
            mine = tuple(int(v) for v in np.array(rgb) + rng.integers(-1, 2, 3))
            mine = (min(mine[0], 100), 100 if rgb[1] == 100 else mine[1], min(mine[2], 100))
            self.assertEqual(self.sample(ctx, f"10.{start + i}.0.1", rgb=mine, **kw), 200, i)
            if (i + 1) % per_day == 0:
                ctx.clock.t += DAY
        return ctx

    def assertRgb(self, rgb, want=(0.97, 1.0, 0.94), delta=0.015):
        for got, expected in zip(rgb, want):
            self.assertAlmostEqual(got, expected, delta=delta)

    def recipe(self, ctx, query=KEY):
        status, body, _ = call(ctx, "monitor_settings", method="GET", query=query, ip="203.0.113.50")
        self.assertEqual(status, 200)
        return body

    # --- what is stored
    def test_sample_is_stored_with_the_servers_own_measurement(self):
        ctx = make_ctx()
        self.assertEqual(self.sample(ctx, "10.0.0.1"), 200)
        row = next(iter(ctx.store.settings_samples.values()))
        self.assertEqual((row["edid_manufacturer"], row["edid_product_code"], row["picture_mode"]), ("AUS", 43537, "srgb"))
        self.assertTrue(row["calibrated"])
        self.assertEqual((row["rgb_r"], row["rgb_g"], row["rgb_b"], row["slider_max"]), (97, 100, 94, 100))
        self.assertLess(row["white_de"], 1.0)  # recomputed from the readings: the monitor is on target
        self.assertAlmostEqual(row["white_cct"], 6504, delta=120)
        self.assertEqual(row["session_month"], "2026-10-01")
        self.assertEqual(len(row["contributor_hash"]), 64)
        off = make_ctx()
        self.sample(off, "10.0.0.2", good=False)
        self.assertGreater(next(iter(off.store.settings_samples.values()))["white_de"], 2.0)
        # The same session also counts toward the phone correction.
        self.assertEqual(len(ctx.store.submissions), 1)

    def test_a_claimed_result_cannot_be_sent(self):
        ctx = make_ctx()
        body = self.payload(ctx, charts=copy.deepcopy(self.off_charts),
                            settings={"calibrated": True, "rgb": {"r": 97, "g": 100, "b": 94, "max": 100}, "white_de": 0.1})
        self.assertEqual(call(ctx, "submissions", body=body)[0], 400)  # no such field
        self.assertFalse(ctx.store.settings_samples)

    def test_settings_need_a_named_model_a_mode_and_a_bright_chart(self):
        ctx = make_ctx()
        ok = {"calibrated": True, "rgb": {"r": 97, "g": 100, "b": 94, "max": 100}}
        base = copy.deepcopy(self.good_charts)
        cases = {
            "no mode": dict(target_monitor={k: v for k, v in TARGET_MONITOR.items() if k != "picture_mode"}),
            "no model code": dict(target_monitor={"screen_tech": "woled", "picture_mode": "srgb"}),
            "no target monitor": dict(target_monitor=None),
            "above the maximum": dict(settings={"calibrated": True, "rgb": {"r": 120, "g": 100, "b": 94, "max": 100}}),
            "zero": dict(settings={"calibrated": True, "rgb": {"r": 0, "g": 100, "b": 94, "max": 100}}),
            "not a flag": dict(settings={"calibrated": "yes", "rgb": ok["rgb"]}),
            "extra field": dict(settings=dict(ok, note="hi")),
            "no bright chart": dict(charts=[c for c in base if (c["type"], c["role"]) != ("bright", "target")]),
        }
        for i, (name, over) in enumerate(cases.items()):
            body = self.payload(ctx, ip=f"192.0.2.{i + 1}", charts=copy.deepcopy(base), settings=ok)
            for k, v in over.items():
                if v is None:
                    body.pop(k)
                else:
                    body[k] = v
            self.assertEqual(call(ctx, "submissions", body=body, ip=f"192.0.2.{i + 1}")[0], 400, name)
        self.assertFalse(ctx.store.settings_samples)

    # --- when a suggestion is shown
    def test_nothing_is_shown_without_enough_independent_samples(self):
        ctx = make_ctx()
        self.fill(ctx, 4)
        self.assertEqual(self.recipe(ctx), {"found": False})  # 4 people: not enough, and no count is revealed
        self.fill(ctx, 1, start=10, per_day=1)
        found = self.recipe(ctx)
        self.assertTrue(found["found"])
        self.assertEqual(found["contributors"], 5)
        self.assertRgb(found["rgb"])
        self.assertIsNone(found["baseline"])

    def test_needs_two_different_days(self):
        ctx = make_ctx()
        self.fill(ctx, 6, per_day=100)  # everyone on one day
        self.assertEqual(self.recipe(ctx), {"found": False})
        ctx.clock.t += DAY
        self.sample(ctx, "10.99.0.1")
        self.assertTrue(self.recipe(ctx)["found"])

    def test_one_network_is_one_voice(self):
        ctx = make_ctx()
        for i in range(8):
            self.sample(ctx, f"10.7.0.{i + 1}")  # one household
            ctx.clock.t += DAY
        self.assertEqual(self.recipe(ctx), {"found": False})

    def test_only_settings_that_worked_count(self):
        ctx = make_ctx()
        self.fill(ctx, 3, good=True)
        self.fill(ctx, 6, start=20, good=False, rgb=(80, 100, 70))  # people who claim to be calibrated but measure 4+ dE off
        self.assertEqual(self.recipe(ctx), {"found": False})
        self.fill(ctx, 2, start=40)
        found = self.recipe(ctx)
        self.assertEqual(found["contributors"], 5)
        self.assertLessEqual(found["typical_de"], 2.0)
        self.assertRgb(found["rgb"])  # the failed attempts didn't move it

    def test_people_who_disagree_are_not_similar_cases(self):
        ctx = make_ctx()
        for i, rgb in enumerate([(97, 100, 94), (100, 90, 80), (85, 100, 100), (100, 100, 70), (90, 100, 100), (100, 80, 100)]):
            self.sample(ctx, f"10.{i}.0.1", rgb=rgb)
            ctx.clock.t += DAY
        self.assertEqual(self.recipe(ctx), {"found": False})

    def test_values_are_compared_as_ratios_of_the_largest(self):
        ctx = make_ctx()
        for i, (rgb, mx) in enumerate([((97, 100, 94), 100), ((194, 200, 188), 200), ((48.5, 50, 47), 50), ((97, 100, 94), 100),
                                       ((485, 500, 470), 1000), ((90, 93, 87.5), 100)]):
            self.sample(ctx, f"10.{i}.0.1", rgb=rgb, mx=mx)
            ctx.clock.t += DAY
        found = self.recipe(ctx)
        self.assertTrue(found["found"])
        self.assertRgb(found["rgb"])

    def test_other_monitors_and_modes_are_other_cases(self):
        ctx = make_ctx()
        self.fill(ctx, 6)
        self.assertTrue(self.recipe(ctx)["found"])
        self.assertEqual(self.recipe(ctx, "monitor=AUS:43537&mode=game"), {"found": False})
        self.assertEqual(self.recipe(ctx, "monitor=AUS:43538&mode=srgb"), {"found": False})
        self.assertEqual(self.recipe(ctx, "monitor=DEL:43537&mode=srgb"), {"found": False})

    def test_what_people_measured_untouched_is_added_when_there_is_enough(self):
        ctx = make_ctx()
        self.fill(ctx, 6)
        self.fill(ctx, 4, start=30, calibrated=False, good=False, rgb=(100, 100, 100))
        self.assertIsNone(self.recipe(ctx)["baseline"])  # 4 people are not enough
        self.fill(ctx, 2, start=50, calibrated=False, good=False, rgb=(100, 100, 100))
        base = self.recipe(ctx)["baseline"]
        self.assertEqual(base["contributors"], 6)
        self.assertAlmostEqual(base["cct"], 6960, delta=150)
        self.assertGreater(base["de"], 2)
        # People who left it as it was never shape the suggested numbers.
        self.assertRgb(self.recipe(ctx)["rgb"])

    def test_untouched_samples_alone_suggest_nothing(self):
        ctx = make_ctx()
        self.fill(ctx, 8, calibrated=False, good=False, rgb=(100, 100, 100))
        self.assertEqual(self.recipe(ctx), {"found": False})

    def test_deleting_by_code_removes_the_sample(self):
        ctx = make_ctx()
        self.fill(ctx, 5)
        code = secrets.token_bytes(16)
        self.assertEqual(self.sample(ctx, "10.77.0.1", rgb=(60, 100, 60), deletion_hash=hashlib.sha256(code).hexdigest()), 200)
        self.assertEqual(len(ctx.store.settings_samples), 6)
        call(ctx, "submissions_delete", body={"code": code.hex()}, ip="203.0.113.77")
        self.assertEqual(len(ctx.store.settings_samples), 5)
        self.assertEqual(len(ctx.store.submissions), 5)

    def test_query_validation_and_limits(self):
        ctx = make_ctx()
        for q in ("", "monitor=AUS:1", "mode=srgb", "monitor=bad&mode=srgb", "monitor=AUS:1&mode=weird",
                  "monitor=AUS:1&mode=srgb&x=1", "monitor=AUS:1&monitor=AUS:2&mode=srgb"):
            self.assertEqual(call(ctx, "monitor_settings", method="GET", query=q)[0], 400, q)
        codes = [call(ctx, "monitor_settings", method="GET", query=KEY, ip="192.0.2.20")[0] for _ in range(62)]
        self.assertEqual(codes[-1], 429)

    def test_network_hashes_expire_but_samples_stay(self):
        ctx = make_ctx()
        self.fill(ctx, 6)
        ctx.clock.t += 61 * DAY
        ctx.store.purge(ctx.now(), recipes.aggregate.hashing.day_of(ctx.now()), "")
        self.assertTrue(all(s["contributor_hash"] is None for s in ctx.store.settings_samples.values()))
        self.assertTrue(self.recipe(ctx)["found"])


class Client(unittest.TestCase):
    GOOD = {"found": True, "rgb": [0.97, 1.0, 0.94], "contributors": 7, "spread": 0.02, "typical_de": 1.1,
            "baseline": {"cct": 6900, "de": 4.2, "contributors": 6}}

    def test_good_answer(self):
        r = shared.check_recipe(self.GOOD)
        self.assertEqual((r["rgb"], r["contributors"], r["baseline"]["cct"]), ([0.97, 1.0, 0.94], 7, 6900.0))

    def test_the_app_insists_on_its_own_minimum(self):
        self.assertIsNone(shared.check_recipe(dict(self.GOOD, contributors=4)))
        self.assertIsNone(shared.check_recipe(dict(self.GOOD, contributors=True)))
        # An unconvincing baseline is dropped; the suggestion stays.
        self.assertIsNone(shared.check_recipe(dict(self.GOOD, baseline={"cct": 6900, "de": 4, "contributors": 2}))["baseline"])

    def test_hostile_answers_are_ignored(self):
        bad = [dict(self.GOOD, found=False), dict(self.GOOD, rgb=[0.5, 0.5, 0.5]), dict(self.GOOD, rgb=[1, 1]), dict(self.GOOD, rgb=[1, 1, "x"]),
               dict(self.GOOD, rgb=[1e9, 1, 1]), dict(self.GOOD, rgb=[float("nan"), 1, 1]), dict(self.GOOD, typical_de=50),
               dict(self.GOOD, spread=5), dict(self.GOOD, contributors=10**9), [], "x", None]
        for answer in bad:
            self.assertIsNone(shared.check_recipe(answer), answer)

    def test_settings_are_cleaned(self):
        ok = privacy.clean_settings({"calibrated": False, "rgb": {"r": 100, "g": 99.96, "b": 98, "max": 100}, "note": "x"})
        self.assertEqual(ok, {"calibrated": False, "rgb": {"r": 100.0, "g": 100.0, "b": 98.0, "max": 100.0}})
        for bad in (None, {}, {"calibrated": 1, "rgb": {}}, {"calibrated": True, "rgb": {"r": 5, "g": 5, "b": 5}},
                    {"calibrated": True, "rgb": {"r": 101, "g": 5, "b": 5, "max": 100}},
                    {"calibrated": True, "rgb": {"r": 0, "g": 5, "b": 5, "max": 100}},
                    {"calibrated": True, "rgb": {"r": float("inf"), "g": 5, "b": 5, "max": 100}}):
            self.assertIsNone(privacy.clean_settings(bad), bad)


if __name__ == "__main__":
    unittest.main()
