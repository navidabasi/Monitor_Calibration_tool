"""Local web server: serves the site and analyzes uploaded RAW photos.

Binds to 127.0.0.1 only. Photos and results are kept in sessions/current/.

Other websites open in the same browser can still send requests to
127.0.0.1, so every request is checked:
  - the Host header must be this server (blocks DNS rebinding),
  - a foreign Origin is rejected (blocks cross-site requests),
  - API calls must carry the per-launch token that is written into
    index.html when it's served, which only same-origin pages can read.
"""

import hmac
import json
import mimetypes
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import analyze, chart, detect, edid, icc, privacy, profiles, raw_io, shared

WEB = Path(__file__).resolve().parent / "web"
SESSION = None  # set by make_server()
MAX_UPLOAD = 300 * 1024 * 1024
TARGETS = {"2.2", "2.4", "srgb"}
RESULT_VERSION = 5  # bump when detect.measure() output changes; older results are re-measured

_lock = threading.Lock()
_sharing_now = set()  # signatures of sessions being sent right now, so a repeat request doesn't send twice


def _load():
    f = SESSION / "session.json"
    if not f.exists():
        return {"next": 1, "photos": []}
    state = json.loads(f.read_text(encoding="utf-8"))
    stale = [p for p in state["photos"] if p.get("result") and p.get("version", 1) < RESULT_VERSION]
    for p in stale:
        file = SESSION / "photos" / p["file"]
        if file.exists():
            p["result"], p["error"] = process_file(file)
        p["version"] = RESULT_VERSION
    if stale:
        _save(state)
    return state


def _save(state):
    SESSION.mkdir(parents=True, exist_ok=True)
    tmp = SESSION / "session.json.tmp"
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(SESSION / "session.json")


def _summary(photo):
    res = photo.get("result") or {}
    ident = analyze.identity(photo)
    return {
        "id": photo["id"],
        "name": photo["name"],
        "error": photo.get("error"),
        "type": ident[0] if ident else None,
        "role": ident[1] if ident else None,
        "detected": bool(res.get("chart")),
        "assigned": bool(photo.get("assigned")),
        "warnings": res.get("warnings", []),
        "shutter": (res.get("exposure") or {}).get("shutter"),
        "iso": (res.get("exposure") or {}).get("iso"),
        "chartFraction": res.get("chartFraction"),
    }


def _phone_of(photos):
    """Who took these photos: the camera key from the first photo that says."""
    for p in photos:
        key = privacy.phone_key((p.get("result") or {}).get("capture"))
        if key:
            return key
    return None


def _has_reference(photos):
    return any(p.get("result") and analyze.identity(p) == ("bright", "reference") for p in photos)


def _read_file(name, default):
    try:
        return json.loads((SESSION.parent / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_file(name, obj):
    folder = SESSION.parent
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (name + ".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    tmp.replace(folder / name)


def _sharing():
    """Did the user agree to share? True, False, or None if they haven't been asked yet."""
    value = _read_file("settings.json", {}).get("share")
    return value if isinstance(value, bool) else None


def _find_correction(photos, names):
    """A correction for this phone and the screen being measured, for sessions with no reference shot.

    Returns (correction or None, state). A correction saved on this computer wins over the shared one.
    State says what happened, for the Results page: None (a reference shot makes this unnecessary),
    "local", "found", "none", "off", "unconfigured", "unknown_screen" or "error".
    """
    if _has_reference(photos):
        return None, None
    phone = _phone_of(photos)
    mon_id, tech = profiles.screen_of((names.get("monitors") or {}).get("target"))
    if not phone:
        return None, "none"
    if tech == "unknown" and not mon_id:
        return None, "unknown_screen"
    local = profiles.find(SESSION.parent / "profiles", phone, tech, mon_id)
    if local:
        return local, "local"
    if not shared.configured():
        return None, "unconfigured"
    if _sharing() is not True:
        return None, "off"
    found, state = shared.lookup(phone, mon_id, tech)
    if not found:
        return None, state
    what = privacy.phone_label(phone)
    found["label"] = f"the shared correction for {what} on {profiles.SCREEN_TECH_LABELS.get(tech, tech)}"
    return found, "found"


def _remember_correction(photos, names, analysis):
    """After a good reference session, keep what it taught about this phone and the reference screen."""
    corr = analysis.get("correction")
    phone = _phone_of(photos)
    mon_id, tech = profiles.screen_of((names.get("monitors") or {}).get("reference"))
    if not corr or not corr["good"] or not phone or (tech == "unknown" and not mon_id):
        return False
    return profiles.save_for_both(SESSION.parent / "profiles", phone, mon_id, tech, corr["k"])


def _session_json(state, target, names):
    photos = state["photos"]
    correction, corr_state = _find_correction(photos, names)
    try:
        analysis = analyze.analyze_session(photos, target, names, names.get("spec"), correction=correction)
    except Exception:  # keep the page usable and show the problem
        traceback.print_exc()
        analysis = {"warnings": ["The analysis hit an unexpected error. See the terminal for details."], "rounds": [], "advice": []}
    saved = False
    if analysis.get("correction"):
        saved = _remember_correction(photos, names, analysis)
        c = analysis["correction"]
        # The matrices stay on the server; the page only needs to know whether the shot was good enough.
        analysis["correction"] = {"good": c["good"], "problems": c["problems"], "saved": saved}
    phone = _phone_of(photos)
    analysis["phone"] = {"label": privacy.phone_label(phone)} if phone else None
    analysis["sharedState"] = corr_state
    return {"photos": [_summary(p) for p in photos], "analysis": analysis}


def _safe_name(name):
    name = Path(unquote(name or "photo.dng")).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80] or "photo.dng"


def process_file(path):
    """Measure one photo file. Returns (result, error)."""
    try:
        image, meta = raw_io.load_raw(path)
        return detect.measure(image, meta), None
    except (raw_io.NotRawError, detect.ChartNotFound) as e:
        return None, str(e)
    except Exception as e:  # unexpected: log it, report briefly
        traceback.print_exc()
        return None, f"Couldn't analyze this photo ({type(e).__name__}). See the terminal for details."


class Handler(BaseHTTPRequestHandler):
    server_version = "MonitorCalibration/1.0"

    def log_message(self, fmt, *args):  # quieter console: only API calls
        if args and isinstance(args[0], str) and "/api/" in args[0]:
            super().log_message(fmt, *args)

    def log_error(self, fmt, *args):
        super().log_message(fmt, *args)

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # No other site may frame this page (stops click-tricking into "Start over").
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")

    def _json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self, api):
        """Reject requests that didn't come from this app's own page."""
        port = self.server.server_address[1]
        if (self.headers.get("Host") or "").lower() not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return False
        if not api:
            return True
        if (self.headers.get("Sec-Fetch-Site") or "same-origin") != "same-origin":
            return False
        return hmac.compare_digest(self.headers.get("X-MC-Token") or "", self.server.token)

    def _blocked(self, api):
        if api:
            # Most likely cause for a real user: the server was restarted, so the page's token is stale.
            return self._json({"error": "This page is out of date. Reload it."}, HTTPStatus.FORBIDDEN)
        body = b"Forbidden"
        self.send_response(HTTPStatus.FORBIDDEN)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _params(self):
        q = parse_qs(urlparse(self.path).query)
        target = q.get("target", ["2.2"])[0]
        names = {"reference": q.get("ref", [""])[0][:60], "target": q.get("tgt", [""])[0][:60]}
        try:
            names["spec"] = json.loads(q.get("refspec", ["{}"])[0][:2000])
        except ValueError:
            names["spec"] = None
        names["monitors"] = {}
        try:
            mons = json.loads(q.get("mon", ["{}"])[0][:4000])
            for role in ("reference", "target"):
                names["monitors"][role] = privacy.clean_monitor(mons.get(role))
        except (ValueError, AttributeError):
            pass
        return q, (target if target in TARGETS else "2.2"), names

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._allowed(api=path.startswith("/api/")):
            return self._blocked(api=path.startswith("/api/"))
        if path == "/api/layout":
            return self._json(chart.layout_json())
        if path == "/api/settings":
            return self._json({"share": _sharing(), "apiConfigured": shared.configured()})
        if path == "/api/contributions":
            items = _read_file("contributions.json", {}).get("items", [])
            return self._json({"items": [{k: v for k, v in i.items() if k != "sig"} for i in items]})
        if path == "/api/reference-profile":
            _, _, names = self._params()
            return self._reference_profile(names)
        if path == "/api/recipe":
            _, _, names = self._params()
            return self._recipe(names)
        if path == "/api/devices":
            return self._json({"monitors": [privacy.clean_monitor(m) for m in edid.read_all()]})
        if path == "/api/session":
            _, target, names = self._params()
            with _lock:
                return self._json(_session_json(_load(), target, names))
        self._static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._allowed(api=True):
            return self._blocked(api=True)
        q, target, names = self._params()
        if path == "/api/icc":
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 10 * 1024 * 1024:
                return self._json({"error": "The file is empty or too large for an ICC profile."}, HTTPStatus.BAD_REQUEST)
            try:
                return self._json(icc.parse(self.rfile.read(length)))
            except icc.ICCError as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception:
                traceback.print_exc()
                return self._json({"error": "Couldn't read this ICC profile."}, HTTPStatus.BAD_REQUEST)
        if path == "/api/settings":
            body = self._body_json()
            if not isinstance(body, dict) or not isinstance(body.get("share"), bool):
                return self._json({"error": "Expected {\"share\": true or false}."}, HTTPStatus.BAD_REQUEST)
            with _lock:
                _write_file("settings.json", {"share": body["share"]})
                shared.forget_lookups()
            return self._json({"share": body["share"], "apiConfigured": shared.configured()})
        m = re.fullmatch(r"/api/contributions/([0-9a-f]{8})/delete", path)
        if m:
            return self._delete_contribution(m.group(1))
        if path == "/api/share":
            return self._share(target, names)
        if path == "/api/share-settings":
            return self._share_settings(target, names)
        with _lock:
            state = _load()
            if path == "/api/upload":
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_UPLOAD:
                    return self._json({"error": "File is empty or too large."}, HTTPStatus.BAD_REQUEST)
                data = self.rfile.read(length)
                pid = state["next"]
                state["next"] += 1
                name = _safe_name(q.get("name", [""])[0])
                folder = SESSION / "photos"
                folder.mkdir(parents=True, exist_ok=True)
                file = folder / f"{pid:03d}_{name}"
                file.write_bytes(data)
                result, error = process_file(file)
                state["photos"].append({"id": pid, "name": name, "file": file.name, "uploaded": time.time(),
                                        "result": result, "error": error, "version": RESULT_VERSION})
                _save(state)
                return self._json(_session_json(state, target, names))

            m = re.fullmatch(r"/api/photo/(\d+)/(assign|delete)", path)
            if m:
                pid, action = int(m.group(1)), m.group(2)
                photo = next((p for p in state["photos"] if p["id"] == pid), None)
                if photo is None:
                    return self._json({"error": "No such photo."}, HTTPStatus.NOT_FOUND)
                if action == "delete":
                    state["photos"].remove(photo)
                    (SESSION / "photos" / photo["file"]).unlink(missing_ok=True)
                else:
                    t, r = q.get("type", [""])[0], q.get("role", [""])[0]
                    if t in chart.TYPES and r in chart.ROLES:
                        photo["assigned"] = {"type": t, "role": r}
                    else:
                        photo.pop("assigned", None)
                _save(state)
                return self._json(_session_json(state, target, names))

            if path == "/api/reset":
                if SESSION.exists():
                    archive = SESSION.parent / time.strftime("previous-%Y%m%d-%H%M%S")
                    shutil.move(str(SESSION), str(archive))
                return self._json(_session_json(_load(), target, names))
        self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def _body_json(self, limit=4096):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return None

    def _delete_contribution(self, cid):
        with _lock:
            item = next((i for i in _read_file("contributions.json", {}).get("items", []) if i["id"] == cid), None)
        if item is None:
            return self._json({"error": "No such contribution."}, HTTPStatus.NOT_FOUND)
        try:
            shared.delete(item["code"])  # network: not under the session lock
        except shared.SharedError as e:
            return self._json({"error": str(e)}, HTTPStatus.BAD_GATEWAY)
        with _lock:
            data = _read_file("contributions.json", {})
            data["items"] = [i for i in data.get("items", []) if i["id"] != cid]
            _write_file("contributions.json", data)
            shared.forget_lookups()
        return self._json({"items": [{k: v for k, v in i.items() if k != "sig"} for i in data["items"]]})

    def _share(self, target, names, settings=None):
        """Send this session's measurements to the shared database, once, if the user agreed and it's a good reference session.

        With `settings` (what the person said about the monitor's own settings) it also carries those."""
        with _lock:
            if _sharing() is not True or not shared.configured():
                return self._json({"status": "off"})
            built = shared.build_payload(_load()["photos"], names.get("spec"), names.get("monitors"), RESULT_VERSION, settings=settings)
            if built is None:
                return self._json({"status": "nothing"})
            payload, code, signature = built
            if signature in _sharing_now or any(i["sig"] == signature for i in _read_file("contributions.json", {}).get("items", [])):
                return self._json({"status": "already"})
            _sharing_now.add(signature)
        try:
            shared.contribute(payload)  # network and proof of work: not under the session lock
        except shared.SharedError as e:
            return self._json({"status": "error", "message": str(e)})
        finally:
            with _lock:
                _sharing_now.discard(signature)
        phone = privacy.phone_key(payload["capture"])
        tech = payload["reference_monitor"].get("screen_tech", "unknown")
        if settings is not None:
            tgt = payload["target_monitor"]
            label = (f"Monitor settings for {tgt.get('model_name') or tgt['manufacturer'] + ':' + str(tgt['product_code'])}"
                     f" · {tgt['picture_mode']} mode · {'calibrated' if payload['settings']['calibrated'] else 'left as it was'}")
        else:
            label = f"{privacy.phone_label(phone)} on {profiles.SCREEN_TECH_LABELS.get(tech, tech)}"
        with _lock:
            data = _read_file("contributions.json", {})
            items = data.setdefault("items", [])
            items.append({"id": code.hex()[:8], "code": code.hex(), "sig": signature, "month": payload["session_month"], "label": label})
            _write_file("contributions.json", data)
        return self._json({"status": "shared", "items": [{k: v for k, v in i.items() if k != "sig"} for i in items]})

    def _share_settings(self, target, names):
        body = self._body_json()
        settings = privacy.clean_settings(body)
        if settings is None:
            return self._json({"error": "Enter the Red, Green and Blue values (each above 0 and not above the slider maximum)."},
                              HTTPStatus.BAD_REQUEST)
        return self._share(target, names, settings=settings)

    def _reference_profile(self, names):
        """Measured primaries for the reference monitor model in its picture mode, if enough people agree."""
        ref = (names.get("monitors") or {}).get("reference") or {}
        mon_id, _ = profiles.screen_of(ref)
        mode = ref.get("picture_mode")
        if not shared.configured():
            return self._json({"state": "unconfigured"})
        if _sharing() is not True:
            return self._json({"state": "off"})
        if not mon_id or not mode:
            return self._json({"state": "unknown"})
        found, state = shared.lookup_reference(mon_id, mode)
        return self._json({"state": state, "profile": found})

    def _recipe(self, names):
        """Suggested settings for the monitor being calibrated, only if enough people with the same model and mode agree."""
        mon_id, _ = profiles.screen_of((names.get("monitors") or {}).get("target"))
        mode = ((names.get("monitors") or {}).get("target") or {}).get("picture_mode")
        if not shared.configured():
            return self._json({"state": "unconfigured"})
        if _sharing() is not True:
            return self._json({"state": "off"})
        if not mon_id or not mode:
            return self._json({"state": "unknown"})
        found, state = shared.lookup_recipe(mon_id, mode)
        return self._json({"state": state, "recipe": found})

    def _static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        file = (WEB / rel).resolve()
        if WEB not in file.parents or not file.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = file.read_bytes()
        if file.name == "index.html":
            body = body.replace(b"__MC_TOKEN__", self.server.token.encode("ascii"))
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # always serve the current version
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second program bind the same port, so a
    # taken port would go unnoticed (or be hijacked). Claim it exclusively instead.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(port, data_dir):
    """Server on 127.0.0.1:port keeping sessions in data_dir/current. Raises OSError if the port is taken."""
    global SESSION
    SESSION = Path(data_dir).resolve() / "current"
    mimetypes.add_type("application/javascript", ".js")
    server = _Server(("127.0.0.1", port), Handler)
    server.token = secrets.token_urlsafe(32)  # new on every launch
    return server
