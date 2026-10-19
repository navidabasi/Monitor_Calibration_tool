"""The local app's side of the shared database: look up corrections, contribute sessions, delete them.

Calls go from this Python process to the public API (never from the page), so
the browser makes no third-party requests. Answers from the API are untrusted:
every number is range-checked before it's used and nothing is ever evaluated.

Sharing is opt-in. Nothing here runs unless the user said yes in Settings,
and nothing is sent unless the API address is configured.
"""

import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, analyze, privacy, submission

# The hosted API. Empty until a domain exists; MONCAL_API_URL overrides it (for development).
DEFAULT_API_URL = ""
TIMEOUT = 15
LOOKUP_TIMEOUT = 5
MAX_ANSWER = 256 * 1024
CACHE_SECONDS = 600
FAILURE_CACHE_SECONDS = 60
MAX_CONTRIBUTORS = 10**7

_cache = {}


class SharedError(Exception):
    """Something went wrong talking to the shared database. The message is safe to show."""


def api_url():
    url = (os.environ.get("MONCAL_API_URL") or DEFAULT_API_URL).strip().rstrip("/")
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        return ""  # never talk to a remote server over plain http
    return url


def configured():
    return bool(api_url())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _request(method, path, body=None, timeout=TIMEOUT):
    base = api_url()
    if not base:
        raise SharedError("The shared database isn't set up.")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={
        "Content-Type": "application/json", "User-Agent": f"monitor-calibration/{__version__}"})
    try:
        with _opener.open(req, timeout=timeout) as resp:  # noqa: S310 - scheme checked in api_url()
            raw = resp.read(MAX_ANSWER + 1)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise SharedError("The shared database is busy. Try again later.") from None
        raise SharedError("The shared database turned the request down.") from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise SharedError("Couldn't reach the shared database.") from None
    if len(raw) > MAX_ANSWER:
        raise SharedError("The shared database sent an unexpected answer.")
    try:
        return json.loads(raw, parse_constant=_reject)
    except ValueError:
        raise SharedError("The shared database sent an unexpected answer.") from None


def _reject(name):
    raise ValueError(name)


# ---------------------------------------------------------------- lookup

def _num(v, lo, hi):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def check_correction(answer):
    """The correction in an API answer as analyze_session() takes it, or None. Everything is checked."""
    if not isinstance(answer, dict) or answer.get("found") is not True:
        return None
    k = answer.get("matrix")
    if (not isinstance(k, list) or len(k) != 3 or not all(isinstance(r, list) and len(r) == 3 for r in k)
            or not all(_num(v, -1e3, 1e3) for r in k for v in r) or not analyze.valid_correction(k)):
        return None
    if answer.get("scope") not in ("monitor", "technology"):
        return None
    contributors, spread = answer.get("contributors"), answer.get("spread_k")
    if not (isinstance(contributors, int) and not isinstance(contributors, bool) and 3 <= contributors <= MAX_CONTRIBUTORS):
        return None
    if not _num(spread, 0, 5000):
        return None
    # A correction that moves a D65 white by thousands of kelvin isn't believable.
    if abs(analyze.white_after_correction(k)[0] - analyze.white_after_correction([[1, 0, 0], [0, 1, 0], [0, 0, 1]])[0]) > 2500:
        return None
    return {"k": [[float(v) for v in r] for r in k], "source": "shared", "scope": answer["scope"],
            "contributors": contributors, "spreadK": float(spread)}


def lookup(phone, monitor_id, tech, now=time.time):
    """(correction or None, state). State: "found", "none" or "error". Cached for a few minutes."""
    if not phone:
        return None, "none"
    key = json.dumps([phone, monitor_id, tech], sort_keys=True)
    hit = _cache.get(key)
    if hit and hit[0] > now():
        return hit[1], hit[2]
    query = dict(phone, tech=tech)
    if monitor_id:
        query["monitor"] = monitor_id
    try:
        answer = _request("GET", "/corrections?" + urllib.parse.urlencode(query), timeout=LOOKUP_TIMEOUT)
        corr = check_correction(answer)
        result, state, ttl = corr, "found" if corr else "none", CACHE_SECONDS
    except SharedError:
        result, state, ttl = None, "error", FAILURE_CACHE_SECONDS
    _cache[key] = (now() + ttl, result, state)
    return result, state


def forget_lookups():
    _cache.clear()


# ---------------------------------------------------------------- contribute

def build_payload(photos, spec, monitors, analysis_version, settings=None, now=time.localtime):
    """(payload without the challenge, deletion code bytes, session signature) or None if there's nothing worth sharing.

    Only allow-listed fields go in: capture tags cleaned by privacy.clean_capture, fixed monitor fields, chart numbers.
    `settings` ({"calibrated": bool, "rgb": {r, g, b, max}}) is what the person chose to share about the monitor's
    own settings; it needs a named monitor model, a picture mode and a bright chart of that monitor.
    """
    kind = privacy.reference_kind(spec)  # before cleaning, which drops where the values came from
    spec = privacy.clean_spec(spec)
    if settings is not None:
        settings = privacy.clean_settings(settings)
        if settings is None:
            return None
    built = submission.build(photos, spec, need_round=settings is not None)
    if built is None:
        return None
    charts, summary, analysis = built
    ref = next(p for p in photos if p.get("result") and analyze.identity(p) == ("bright", "reference"))
    capture = privacy.clean_capture(ref["result"].get("capture"))
    if "UniqueCameraModel" not in capture and capture.get("Model"):
        capture["UniqueCameraModel"] = capture["Model"]
    if privacy.phone_key(capture) is None or "ColorMatrix1" not in capture:
        return None
    ref_mon = privacy.clean_monitor((monitors or {}).get("reference"))
    tgt_mon = privacy.clean_monitor((monitors or {}).get("target"))
    if ref_mon.get("screen_tech", "unknown") == "unknown" and not (ref_mon.get("manufacturer") and "product_code" in ref_mon):
        return None  # nobody could say which kind of screen the correction belongs to
    ref_mon.setdefault("screen_tech", "unknown")
    if settings is not None and not (tgt_mon.get("manufacturer") and "product_code" in tgt_mon and tgt_mon.get("picture_mode")):
        return None
    code = secrets.token_bytes(16)
    payload = {
        "v": submission.SCHEMA_VERSION,
        "deletion_hash": hashlib.sha256(code).hexdigest(),
        "app_version": __version__,
        "analysis_version": analysis_version,
        "session_month": time.strftime("%Y-%m", now()),
        "capture": capture,
        "reference_monitor": ref_mon,
        "reference": {"kind": kind, "spec": spec},
        "charts": charts,
        "summary": summary,
    }
    if tgt_mon:
        payload["target_monitor"] = tgt_mon
    if settings is not None:
        payload["settings"] = settings
    # Identifies this session (and these settings), so the same one isn't sent twice.
    signature = hashlib.sha256(json.dumps([[c["patches"] for c in charts], settings], sort_keys=True).encode()).hexdigest()
    return payload, code, signature


MIN_RECIPE_CONTRIBUTORS = 5  # the app's own floor, whatever the server says


def check_recipe(answer):
    """Suggested settings in an API answer, or None. Everything is range-checked, and the app insists on its own
    minimum number of contributors in case the server is wrong or hostile."""
    if not isinstance(answer, dict) or answer.get("found") is not True:
        return None
    rgb = answer.get("rgb")
    if not (isinstance(rgb, list) and len(rgb) == 3 and all(_num(v, 0.05, 1.0) for v in rgb) and max(rgb) >= 0.999):
        return None
    n = answer.get("contributors")
    if not (isinstance(n, int) and not isinstance(n, bool) and MIN_RECIPE_CONTRIBUTORS <= n <= MAX_CONTRIBUTORS):
        return None
    if not (_num(answer.get("spread"), 0, 0.1) and _num(answer.get("typical_de"), 0, 2.0)):
        return None
    base = answer.get("baseline")
    if base is not None:
        if not (isinstance(base, dict) and _num(base.get("cct"), 1000, 20000) and _num(base.get("de"), 0, 100)
                and isinstance(base.get("contributors"), int) and not isinstance(base.get("contributors"), bool)
                and MIN_RECIPE_CONTRIBUTORS <= base["contributors"] <= MAX_CONTRIBUTORS):
            base = None
    return {"rgb": [float(v) for v in rgb], "contributors": n, "spread": float(answer["spread"]),
            "typicalDe": float(answer["typical_de"]),
            "baseline": None if base is None else {"cct": float(base["cct"]), "de": float(base["de"]), "contributors": base["contributors"]}}


def lookup_recipe(monitor_id, mode, now=time.time):
    """(suggestion or None, state) for a monitor model and picture mode. State: "found", "none" or "error"."""
    key = json.dumps(["recipe", monitor_id, mode])
    hit = _cache.get(key)
    if hit and hit[0] > now():
        return hit[1], hit[2]
    try:
        answer = _request("GET", "/monitors/settings?" + urllib.parse.urlencode({"monitor": monitor_id, "mode": mode}),
                          timeout=LOOKUP_TIMEOUT)
        found = check_recipe(answer)
        result, state, ttl = found, "found" if found else "none", CACHE_SECONDS
    except SharedError:
        result, state, ttl = None, "error", FAILURE_CACHE_SECONDS
    _cache[key] = (now() + ttl, result, state)
    return result, state


MIN_PROFILE_CONTRIBUTORS = 5


def check_reference(answer):
    """A measured reference profile in an API answer as an analyzer spec, or None. Range-checked, and the app
    insists on its own minimum number of contributors."""
    if not isinstance(answer, dict) or answer.get("found") is not True:
        return None
    prim, white = answer.get("primaries"), answer.get("white")
    if not (isinstance(prim, list) and len(prim) == 3 and all(isinstance(p, list) and len(p) == 2 for p in prim)
            and isinstance(white, list) and len(white) == 2):
        return None
    pts = prim + [white]
    if not all(_num(v, 0.01, 0.89) for p in pts for v in p):
        return None
    n = answer.get("contributors")
    if not (isinstance(n, int) and not isinstance(n, bool) and MIN_PROFILE_CONTRIBUTORS <= n <= MAX_CONTRIBUTORS):
        return None
    if not _num(answer.get("spread"), 0, 0.02):
        return None
    spec = {"kind": "custom", "source": "shared", "name": f"measured values from {n} people",
            "primaries": [[float(x), float(y)] for x, y in prim], "white": [float(white[0]), float(white[1])]}
    if analyze.reference_spec(spec).get("invalid"):
        return None
    return {"spec": spec, "contributors": n, "spread": float(answer["spread"])}


def lookup_reference(monitor_id, mode, now=time.time):
    """(profile or None, state) for a reference monitor model and picture mode. State: "found", "none" or "error"."""
    key = json.dumps(["reference", monitor_id, mode])
    hit = _cache.get(key)
    if hit and hit[0] > now():
        return hit[1], hit[2]
    try:
        answer = _request("GET", "/monitors/reference?" + urllib.parse.urlencode({"monitor": monitor_id, "mode": mode}),
                          timeout=LOOKUP_TIMEOUT)
        found = check_reference(answer)
        result, state, ttl = found, "found" if found else "none", CACHE_SECONDS
    except SharedError:
        result, state, ttl = None, "error", FAILURE_CACHE_SECONDS
    _cache[key] = (now() + ttl, result, state)
    return result, state


def contribute(payload):
    """Send a payload: ask for a challenge, do the work it asks for, post. Raises SharedError."""
    from . import _pow

    challenge = _request("POST", "/challenge", {})
    try:
        solved = dict(challenge, counter=_pow.solve(challenge))
        if set(challenge) != {"nonce", "difficulty", "expires", "sig"} or not 1 <= challenge["difficulty"] <= _pow.MAX_DIFFICULTY:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise SharedError("The shared database sent an unexpected challenge.") from None
    answer = _request("POST", "/submissions", dict(payload, challenge=solved))
    if not (isinstance(answer, dict) and answer.get("status") == "received"):
        raise SharedError("The shared database sent an unexpected answer.")


def delete(code_hex):
    answer = _request("POST", "/submissions/delete", {"code": code_hex})
    if not (isinstance(answer, dict) and answer.get("status") == "ok"):
        raise SharedError("The shared database sent an unexpected answer.")
