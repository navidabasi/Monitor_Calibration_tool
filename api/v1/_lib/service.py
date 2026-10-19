"""The API endpoints, independent of Vercel: handle() takes a request and returns (status, body, headers).

Every error answer is one of a few fixed messages. Nothing about why a
submission failed, no stack traces and no data from other users is ever
returned.
"""

import hashlib
import hmac
import json
import urllib.parse

from monitor_calibration import analyze, privacy, submission

from . import aggregate, coverage, hashing, proof, recipes, refprofiles, schema

TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# (requests, window) per address hash. Windows are hours ("h") or days ("d").
LIMITS = {
    "challenge": (30, "h"),
    "submissions": (10, "d"),
    "corrections": (60, "h"),
    "monitors_search": (120, "h"),
    "submissions_delete": (20, "h"),
    "monitor_settings": (60, "h"),
    "monitor_reference": (60, "h"),
    "coverage": (30, "h"),
}
ERRORS = {
    400: "bad_request", 403: "forbidden", 404: "not_found", 405: "method_not_allowed", 413: "too_large",
    415: "unsupported_media_type", 422: "rejected", 429: "rate_limited", 500: "server_error",
}
MIN_CONTRIBUTORS_SERVED = aggregate.MIN_CONTRIBUTORS


class HttpError(Exception):
    def __init__(self, status):
        self.status = status


def _reject_constant(name):
    raise ValueError(name)  # NaN, Infinity


def _parse_json(body):
    try:
        return json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HttpError(400) from None


def _answer(status, body=None, extra=None):
    headers = {
        "Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    }
    headers.update(extra or {})
    return status, body if body is not None else {"error": ERRORS.get(status, "error")}, headers


def handle(ctx, route, method, query, headers, body):
    """Run one request. `route` is the endpoint name, `query` the raw query string, `headers` lower-cased."""
    cors = {}
    try:
        origin = headers.get("origin")
        hosted = bool(origin)
        if hosted:
            if not ctx.config.site_origin or origin.rstrip("/") != ctx.config.site_origin:
                raise HttpError(403)
            cors = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
            if method == "OPTIONS":
                return _answer(204, {}, dict(cors, **{"Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                                                      "Access-Control-Allow-Headers": "Content-Type",
                                                      "Access-Control-Max-Age": "600"}))
        if route == "cron_aggregate":
            if method not in ("GET", "POST"):
                raise HttpError(405)
            return _cron_answer(ctx, headers)
        endpoint = _ENDPOINTS.get(route)
        if endpoint is None:
            raise HttpError(404)
        wants = endpoint["method"]
        if method != wants:
            raise HttpError(405)
        if len(body) > ctx.config.max_body:
            raise HttpError(413)
        if wants == "POST" and not headers.get("content-type", "").lower().startswith("application/json"):
            raise HttpError(415)
        ip = hashing.client_ip(headers)
        _rate_limit(ctx, route, ip)
        status, payload = endpoint["fn"](ctx, ip, hosted, query, body)
        if route == "coverage":  # counts only, so it may be cached for an hour
            cors = dict(cors, **{"Cache-Control": "public, max-age=3600"})
        return _answer(status, payload, cors)
    except HttpError as e:
        return _answer(e.status, None, cors)
    except Exception:  # never leak details; the platform's log has the traceback
        import traceback

        traceback.print_exc()
        return _answer(500, None, cors)


def _rate_limit(ctx, route, ip):
    limit, unit = LIMITS[route]
    start = hashing.hour_start(ctx.now()) if unit == "h" else hashing.day_start(ctx.now())
    if not ctx.store.rate_hit(hashing.rate_key(ctx, ip, route), start, limit):
        raise HttpError(429)


# ---------------------------------------------------------------- endpoints

def challenge(ctx, ip, hosted, query, body):
    return 200, proof.issue(ctx)


def _turnstile_ok(ctx, token):
    if not ctx.config.turnstile_secret or not token:
        return False
    try:
        answer = ctx.http_post(TURNSTILE_URL, {"secret": ctx.config.turnstile_secret, "response": token})
    except Exception:
        return False
    return bool(isinstance(answer, dict) and answer.get("success") is True)


def _kind_matches(reference):
    kind, spec = reference["kind"], reference["spec"]
    if kind in ("srgb", "p3", "adobe"):
        return spec.get("name") == kind
    if kind in ("icc", "manual", "database"):
        return spec.get("kind") == "custom"
    return spec.get("name") in ("srgb", "p3")  # a phone or tablet screen shown in its natural mode


def submit(ctx, ip, hosted, query, body):
    try:
        data = schema.submission(_parse_json(body))
    except schema.Invalid:
        raise HttpError(400) from None
    if not _kind_matches(data["reference"]):
        raise HttpError(400)
    if hosted and not _turnstile_ok(ctx, data.get("turnstile")):
        raise HttpError(403)
    if not proof.redeem(ctx, data["challenge"]):
        raise HttpError(403)

    capture = privacy.clean_capture(data["capture"])
    key = privacy.phone_key(capture)
    ref_mon = privacy.clean_monitor(data["reference_monitor"])
    tgt_mon = privacy.clean_monitor(data.get("target_monitor"))
    if key is None or capture.get("UniqueCameraModel") != data["capture"]["UniqueCameraModel"]:
        raise HttpError(422)
    try:
        result = submission.verify(data["charts"], data["reference"]["spec"], data["summary"], capture)
    except submission.Rejected:
        raise HttpError(422) from None

    store = ctx.store
    ref_row = store.upsert_monitor(ref_mon)
    if ref_mon["screen_tech"] == "unknown" and ref_row is None:
        raise HttpError(422)  # a correction can't be filed under a screen nobody can name
    phone = store.upsert_phone(key)
    if phone["status"] == "blocked":
        raise HttpError(422)
    known = phone.get("embedded_color_matrix")
    if known and not _same_matrix(known, capture.get("ColorMatrix1")):
        raise HttpError(422)  # claims to be this phone but carries another phone's color data
    tgt_row = store.upsert_monitor(tgt_mon) if tgt_mon else None
    settings = privacy.clean_settings(data.get("settings")) if "settings" in data else None
    if "settings" in data and (settings is None or not result.get("round")):
        raise HttpError(422)

    store.insert_submission({
        "session_month": data["session_month"] + "-01",
        "app_version": data["app_version"],
        "analysis_version": data["analysis_version"],
        "phone_id": phone["id"],
        "reference_monitor_id": ref_row["id"] if ref_row else None,
        "target_monitor_id": tgt_row["id"] if tgt_row else None,
        "reference_kind": data["reference"]["kind"],
        "reference_screen_tech": ref_mon["screen_tech"],
        "target_picture_mode": tgt_mon.get("picture_mode"),
        "capture": capture,
        "measurements": {"charts": data["charts"], "summary": data["summary"], "spec": data["reference"]["spec"],
                         "reference_monitor": ref_mon, "target_monitor": tgt_mon},
        "server_result": result,
        "status": "pending",
        "contributor_hash": hashing.contributor_hash(ctx, ip),
        "deletion_hash": data["deletion_hash"],
        "received_on": hashing.day_of(ctx.now()),
    })
    _keep_reference_profile(ctx, ip, data, ref_mon)
    if settings:
        rgb, rnd = settings["rgb"], result["round"]
        store.insert_settings_sample({
            "edid_manufacturer": tgt_mon["manufacturer"], "edid_product_code": tgt_mon["product_code"],
            "picture_mode": tgt_mon["picture_mode"], "calibrated": settings["calibrated"],
            "rgb_r": rgb["r"], "rgb_g": rgb["g"], "rgb_b": rgb["b"], "slider_max": rgb["max"],
            "white_cct": round(rnd["cct"], 1), "white_de": round(rnd["dE"], 2), "white_duv": round(rnd["duv"], 4),
            "gamma": None if rnd["gamma"] is None else round(rnd["gamma"], 3), "max_tint": round(rnd["max_tint"], 2),
            "phone_id": phone["id"], "session_month": data["session_month"] + "-01",
            "contributor_hash": hashing.contributor_hash(ctx, ip), "deletion_hash": data["deletion_hash"],
            "received_on": hashing.day_of(ctx.now()),
        })
    return 200, {"status": "received"}


def _keep_reference_profile(ctx, ip, data, ref_mon):
    """A reference monitor's measured primaries (ICC or report values), filed under its model and picture mode."""
    if data["reference"]["kind"] not in ("icc", "manual"):
        return  # "sRGB" is a belief, "database" is somebody else's measurement: neither is a new one
    if not (ref_mon.get("manufacturer") and "product_code" in ref_mon and ref_mon.get("picture_mode")):
        return
    spec = analyze.reference_spec(data["reference"]["spec"])
    if spec.get("invalid"):
        return
    ctx.store.insert_reference_sample({
        "edid_manufacturer": ref_mon["manufacturer"], "edid_product_code": ref_mon["product_code"],
        "picture_mode": ref_mon["picture_mode"],
        "primaries": [round(float(v), 5) for p in spec["primaries"] for v in p], "white": [round(float(v), 5) for v in spec["white"]],
        "session_month": data["session_month"] + "-01", "contributor_hash": hashing.contributor_hash(ctx, ip),
        "deletion_hash": data["deletion_hash"], "received_on": hashing.day_of(ctx.now()),
    })


def _same_matrix(a, b):
    if not a or not b or len(a) != len(b):
        return False
    return all(abs(x - y) <= 1e-3 * max(1.0, abs(x)) for x, y in zip(a, b))


def _flat_query(query):
    """Query string to a dict of single values. Repeated or oversized parameters are errors."""
    if len(query) > 600:
        raise HttpError(400)
    parsed = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=12)
    if any(len(v) != 1 for v in parsed.values()):
        raise HttpError(400)
    return {k: v[0] for k, v in parsed.items()}


def corrections(ctx, ip, hosted, query, body):
    try:
        q = schema.corrections_query(_flat_query(query))
    except schema.Invalid:
        raise HttpError(400) from None
    key = {f: q[f] for f in schema.PHONE_QUERY}
    phone = next((p for p in ctx.store.list_phones() if all(p[f] == key[f] for f in key)), None)
    if phone is None:
        return 200, {"found": False}
    wanted = []
    if q.get("monitor") and q.get("tech"):
        man, code = q["monitor"].split(":")
        mon = next((m for m in ctx.store.list_monitors("verified")
                    if m["edid_manufacturer"] == man and m["edid_product_code"] == int(code)), None)
        if mon:
            wanted.append(("monitor", mon["id"]))
    if q.get("tech"):
        wanted.append(("technology", None))
    for scope, mid in wanted:
        row = ctx.store.get_correction(phone["id"], mid, q["tech"])
        if row and not row["frozen"] and row["contributors"] >= MIN_CONTRIBUTORS_SERVED:
            return 200, {"found": True, "scope": scope, "matrix": row["matrix"], "contributors": row["contributors"],
                         "spread_k": row["spread_k"], "version": row["analysis_version"]}
    return 200, {"found": False}


def monitors_search(ctx, ip, hosted, query, body):
    try:
        q = schema.search_query(_flat_query(query))
    except schema.Invalid:
        raise HttpError(400) from None
    rows = ctx.store.search_monitors(q["q"], 10)
    return 200, {"monitors": [{"name": r["model_name"], "edid": f"{r['edid_manufacturer']}:{r['edid_product_code']}",
                               "screen_tech": r["screen_tech"]} for r in rows if r["status"] == "verified"]}


def monitor_settings(ctx, ip, hosted, query, body):
    """Suggested settings for a monitor model and picture mode, or {"found": false} unless enough people agree."""
    try:
        q = schema.recipe_query(_flat_query(query))
    except schema.Invalid:
        raise HttpError(400) from None
    man, code = q["monitor"].split(":")
    found = recipes.compute(ctx.store, man, int(code), q["mode"])
    if found is None:
        return 200, {"found": False}
    return 200, dict(found, found=True)


def monitor_reference(ctx, ip, hosted, query, body):
    """Measured primaries for a reference monitor model and picture mode, or {"found": false} unless enough people agree."""
    try:
        q = schema.recipe_query(_flat_query(query))
    except schema.Invalid:
        raise HttpError(400) from None
    man, code = q["monitor"].split(":")
    found = refprofiles.compute(ctx.store, man, int(code), q["mode"])
    return 200, ({"found": False} if found is None else dict(found, found=True))


def coverage_counts(ctx, ip, hosted, query, body):
    return 200, coverage.build(ctx.store)


def submissions_delete(ctx, ip, hosted, query, body):
    try:
        data = schema.delete_request(_parse_json(body))
    except schema.Invalid:
        raise HttpError(400) from None
    digest = hashlib.sha256(bytes.fromhex(data["code"])).hexdigest()
    for phone_id in set(ctx.store.delete_by_deletion_hash(digest)):
        aggregate.recompute_phone(ctx, phone_id, tripwire=False)  # removing a contribution never freezes anything
    return 200, {"status": "ok"}  # the same answer whether or not anything matched


def _cron_answer(ctx, headers):
    secret = ctx.config.cron_secret
    given = headers.get("authorization", "")
    if not secret or not hmac.compare_digest(given, f"Bearer {secret}"):
        return _answer(403)
    counts = aggregate.run(ctx)
    return _answer(200, {"status": "ok", **counts})


_ENDPOINTS = {
    "challenge": {"method": "POST", "fn": challenge},
    "submissions": {"method": "POST", "fn": submit},
    "corrections": {"method": "GET", "fn": corrections},
    "monitors_search": {"method": "GET", "fn": monitors_search},
    "submissions_delete": {"method": "POST", "fn": submissions_delete},
    "monitor_settings": {"method": "GET", "fn": monitor_settings},
    "monitor_reference": {"method": "GET", "fn": monitor_reference},
    "coverage": {"method": "GET", "fn": coverage_counts},
}
