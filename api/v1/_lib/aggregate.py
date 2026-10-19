"""Turn quarantined submissions into shared corrections (the hourly job).

Rules (plan section 8):
  * New submissions wait as `pending`.
  * Where a correction already exists, a pending submission is accepted only if
    it agrees with it; otherwise it's `quarantined` for review.
  * For a new key, a group of at least 3 submissions from different network
    prefixes, on at least 2 different days, that agree with each other is
    accepted. Submissions that agree with nobody stay pending and expire.
  * Corrections are the element-wise median of accepted submissions, one vote
    per contributor, and are only published with at least 3 contributors.
  * Tripwires freeze a key for admin review: an aggregate that would move by
    more than 150 K in one run, or more than 20 pending submissions in a day.
"""

from collections import defaultdict

import numpy as np

from monitor_calibration import analyze

from . import hashing

MIN_CONTRIBUTORS = 3
MIN_DAYS = 2
ACCEPT_CCT = 200.0  # K: how far a submission's D65 white may sit from the group's
ACCEPT_MATRIX = 0.12  # largest difference in any element of the correction
FREEZE_SHIFT = 150.0  # K
FREEZE_PENDING_PER_DAY = 20


# ---------------------------------------------------------------- helpers

def matrix_of(sub):
    return np.array(sub["server_result"]["k"], dtype=float).reshape(3, 3)


def cct_of(k):
    return analyze.white_after_correction(k)[0]


def close(a, b):
    return bool(np.abs(a - b).max() <= ACCEPT_MATRIX and abs(cct_of(a) - cct_of(b)) <= ACCEPT_CCT)


def one_vote(subs):
    """The newest submission of each contributor. Submissions whose hash has expired each count once."""
    votes = {}
    for s in sorted(subs, key=lambda s: s["id"]):
        votes[s["contributor_hash"] or ("id", s["id"])] = s
    return list(votes.values())


def median_matrix(subs):
    return np.median(np.stack([matrix_of(s) for s in subs]), axis=0)


def spread_k(subs):
    ccts = np.array([cct_of(matrix_of(s)) for s in subs])
    return float(1.4826 * np.median(np.abs(ccts - np.median(ccts))))


def _row(phone_id, monitor_id, tech, votes, today):
    k = median_matrix(votes)
    versions = [s["analysis_version"] for s in votes]
    return {"phone_id": phone_id, "monitor_id": monitor_id, "screen_tech": tech, "matrix": k.tolist(),
            "contributors": len(votes), "spread_k": round(spread_k(votes), 1), "analysis_version": max(versions),
            "frozen": False, "updated_on": today}


# ---------------------------------------------------------------- the job

def run(ctx):
    """Process everything pending. Returns counts, for logs and tests."""
    store = ctx.store
    today = hashing.day_of(ctx.now())
    counts = {"accepted": 0, "quarantined": 0, "frozen": 0}
    groups = defaultdict(list)
    for s in store.list_submissions(status="pending"):
        groups[(s["phone_id"], s["reference_screen_tech"])].append(s)

    touched = set()
    for (phone_id, tech), pending in groups.items():
        current = store.get_correction(phone_id, None, tech)
        if current and current["frozen"]:
            continue
        if sum(1 for s in pending if s["received_on"] == today) > FREEZE_PENDING_PER_DAY:
            _freeze(store, phone_id, tech, current, today)
            counts["frozen"] += 1
            continue
        accepted, quarantined = _decide(store, phone_id, tech, pending)
        for s in accepted:
            store.update_submission(s["id"], {"status": "accepted"})
        for s in quarantined:
            store.update_submission(s["id"], {"status": "quarantined"})
        counts["accepted"] += len(accepted)
        counts["quarantined"] += len(quarantined)
        if accepted:
            touched.add(phone_id)

    for phone_id in touched:
        counts["frozen"] += recompute_phone(ctx, phone_id, tripwire=True)
    verify_entities(ctx)
    return counts


def _freeze(store, phone_id, tech, current, today):
    row = current or {"phone_id": phone_id, "monitor_id": None, "screen_tech": tech, "matrix": np.eye(3).tolist(),
                      "contributors": 0, "spread_k": 0.0, "analysis_version": 0, "updated_on": today}
    store.put_correction(dict(row, frozen=True))


def _decide(store, phone_id, tech, pending):
    """(accepted, quarantined) among the pending submissions of one phone and screen kind."""
    accepted_before = [s for s in store.list_submissions(status="accepted", phone_id=phone_id)
                       if s["reference_screen_tech"] == tech]
    established = one_vote(accepted_before)
    if len(established) >= MIN_CONTRIBUTORS:
        center = median_matrix(established)
        good = [s for s in pending if close(matrix_of(s), center)]
        return good, [s for s in pending if s not in good]

    votes = one_vote(pending) + [dict(s, contributor_hash=("prior", s["id"])) for s in established]
    best = []
    for v in votes:
        near = [u for u in votes if close(matrix_of(u), matrix_of(v))]
        if len(near) > len(best):
            best = near
    days = {s["received_on"] for s in best}
    if len(best) < MIN_CONTRIBUTORS or len(days) < MIN_DAYS:
        return [], []
    center = median_matrix(best)
    return [s for s in pending if close(matrix_of(s), center)], []


def recompute_phone(ctx, phone_id, tripwire):
    """Rebuild every correction of one phone from its accepted submissions. Returns how many keys got frozen."""
    store = ctx.store
    today = hashing.day_of(ctx.now())
    accepted = store.list_submissions(status="accepted", phone_id=phone_id)
    wanted = {}
    by_tech = defaultdict(list)
    by_monitor = defaultdict(list)
    for s in accepted:
        by_tech[s["reference_screen_tech"]].append(s)
        if s.get("reference_monitor_id"):
            by_monitor[(s["reference_monitor_id"], s["reference_screen_tech"])].append(s)
    for tech, subs in by_tech.items():
        wanted[(None, tech)] = subs
    for (mid, tech), subs in by_monitor.items():
        wanted[(mid, tech)] = subs

    frozen = 0
    for (mid, tech), subs in wanted.items():
        votes = one_vote(subs)
        existing = store.get_correction(phone_id, mid, tech)
        if existing and existing["frozen"]:
            continue
        if len(votes) < MIN_CONTRIBUTORS:
            if existing:
                store.delete_correction(phone_id, mid, tech)
            continue
        row = _row(phone_id, mid, tech, votes, today)
        if tripwire and existing and abs(cct_of(np.array(row["matrix"])) - cct_of(np.array(existing["matrix"]))) > FREEZE_SHIFT:
            store.put_correction(dict(existing, frozen=True))
            frozen += 1
            continue
        store.put_correction(row)

    for c in store.list_corrections(phone_id=phone_id):
        if (c["monitor_id"], c["screen_tech"]) not in wanted and not c["frozen"]:
            store.delete_correction(phone_id, c["monitor_id"], c["screen_tech"])
    return frozen


def verify_entities(ctx):
    """Phones and monitors become public after 3 independent contributors have used them."""
    store = ctx.store
    for phone in store.list_phones("pending"):
        votes = one_vote(store.list_submissions(status="accepted", phone_id=phone["id"]))
        if len(votes) >= MIN_CONTRIBUTORS:
            mats = [v["capture"]["ColorMatrix1"] for v in votes if v["capture"].get("ColorMatrix1")]
            store.update_phone(phone["id"], {"status": "verified",
                                             "embedded_color_matrix": np.median(np.array(mats), axis=0).tolist() if mats else None})
    for mon in store.list_monitors("pending"):
        subs = store.list_submissions(status="accepted", monitor_id=mon["id"])
        if len(one_vote(subs)) >= MIN_CONTRIBUTORS:
            techs = [s["reference_screen_tech"] for s in subs if s.get("reference_monitor_id") == mon["id"]]
            fields = {"status": "verified"}
            if techs:
                fields["screen_tech"] = max(set(techs), key=techs.count)
            store.update_monitor(mon["id"], fields)
