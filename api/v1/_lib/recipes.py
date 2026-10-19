"""Suggested monitor settings, from what people with the same monitor model and picture mode ended up with.

A suggestion is only ever shown when enough independent people agree:

  * at least 5 contributors (one vote per network prefix), on at least 2 different days;
  * only people who said they calibrated it, and whose measurement (recomputed by the
    server from their readings) reached 2 delta E or less;
  * their Red, Green and Blue values, as ratios of the largest of the three, agree within
    4 percentage points (a robust spread). If they don't, the cases aren't similar enough.

Otherwise nothing is returned, not even a count. What people measured with their settings
left as they were is added as a typical starting point, under the same conditions.
"""

import numpy as np

from . import aggregate

MIN_CONTRIBUTORS = 5
MIN_DAYS = 2
MAX_DE = 2.0
MAX_SPREAD = 0.04


def _votes(samples):
    return aggregate.one_vote(samples)


def _enough(votes):
    return len(votes) >= MIN_CONTRIBUTORS and len({v["received_on"] for v in votes}) >= MIN_DAYS


def compute(store, manufacturer, product_code, picture_mode):
    """{"rgb": ratios, "contributors", "spread", "typical_de", "baseline": {...} or None}, or None if there isn't enough."""
    samples = store.list_settings_samples(manufacturer, product_code, picture_mode)
    worked = _votes([s for s in samples if s["calibrated"] and s["white_de"] <= MAX_DE])
    if not _enough(worked):
        return None
    ratios = np.array([[s["rgb_r"], s["rgb_g"], s["rgb_b"]] for s in worked], dtype=float)
    ratios = ratios / ratios.max(axis=1, keepdims=True)
    centre = np.median(ratios, axis=0)
    spread = float((1.4826 * np.median(np.abs(ratios - centre), axis=0)).max())
    if spread > MAX_SPREAD:
        return None
    out = {"rgb": [round(float(v), 3) for v in centre], "contributors": len(worked), "spread": round(spread, 3),
           "typical_de": round(float(np.median([s["white_de"] for s in worked])), 2), "baseline": None}
    untouched = _votes([s for s in samples if not s["calibrated"]])
    if _enough(untouched):
        out["baseline"] = {"cct": round(float(np.median([s["white_cct"] for s in untouched]))),
                           "de": round(float(np.median([s["white_de"] for s in untouched])), 1),
                           "contributors": len(untouched)}
    return out
