"""Measured reference profiles: what people's ICC profiles and calibration-report values say a monitor model shows.

Used to replace "assume this reference shows exact sRGB" when the reference is a known model in a known picture
mode. Only measurements count (an ICC profile or values typed from a report); a person who merely said "sRGB",
or who used the database's own values, adds nothing. A profile is only returned when:

  * at least 5 contributors (one vote per network prefix) on at least 2 different days sent one;
  * the primaries and white point agree: a robust spread of at most 0.01 in CIE x and y.

Otherwise nothing is returned, not even a count.
"""

import numpy as np

from . import aggregate

MIN_CONTRIBUTORS = 5
MIN_DAYS = 2
MAX_SPREAD = 0.01


def compute(store, manufacturer, product_code, picture_mode):
    """{"primaries": [[x, y] x 3], "white": [x, y], "contributors", "spread"} or None."""
    votes = aggregate.one_vote(store.list_reference_samples(manufacturer, product_code, picture_mode))
    if len(votes) < MIN_CONTRIBUTORS or len({v["received_on"] for v in votes}) < MIN_DAYS:
        return None
    pts = np.array([list(v["primaries"]) + list(v["white"]) for v in votes], dtype=float)  # n x 8
    centre = np.median(pts, axis=0)
    spread = float((1.4826 * np.median(np.abs(pts - centre), axis=0)).max())
    if spread > MAX_SPREAD:
        return None
    c = [round(float(v), 4) for v in centre]
    return {"primaries": [c[0:2], c[2:4], c[4:6]], "white": c[6:8], "contributors": len(votes), "spread": round(spread, 4)}
