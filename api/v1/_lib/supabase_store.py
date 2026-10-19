"""The real database: Supabase's REST interface (PostgREST), using the service-role key.

Only the API process holds that key. Rows are never exposed to users: every
table denies the `anon` and `authenticated` roles (see supabase/migrations).
Standard library only, so the serverless bundle stays small.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

PAGE = 1000
SUBMISSION_COLUMNS = (
    "id,session_month,app_version,analysis_version,phone_id,reference_monitor_id,target_monitor_id,reference_kind,"
    "reference_screen_tech,target_picture_mode,capture,server_result,status,contributor_hash,deletion_hash,received_on"
)


class StoreError(Exception):
    pass


def _urlopen_json(method, url, headers, body):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - https URL from our own config
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise StoreError(f"database answered {e.code}") from None


def _bytea(hex_str):
    return None if hex_str is None else "\\x" + hex_str


def _unbytea(value):
    return value[2:] if isinstance(value, str) and value.startswith("\\x") else value


def eq(value):
    return f"eq.{value}"


class SupabaseStore:
    def __init__(self, url, service_key, request=None):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}", "Content-Type": "application/json"}
        self._request = request or _urlopen_json

    # --- plumbing
    def _call(self, method, path, query=None, body=None, prefer=None, extra=None):
        url = f"{self.base}/{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        headers = dict(self.headers, **(extra or {}))
        if prefer:
            headers["Prefer"] = prefer
        return self._request(method, url, headers, body)

    def _select(self, table, filters=None, columns="*", limit=None, order=None):
        query = dict(filters or {}, select=columns)
        if order:
            query["order"] = order
        if limit:
            query["limit"] = limit
            return self._call("GET", table, query) or []
        rows, start = [], 0
        while True:
            page = self._call("GET", table, query, extra={"Range-Unit": "items", "Range": f"{start}-{start + PAGE - 1}"}) or []
            rows += page
            if len(page) < PAGE:
                return rows
            start += PAGE

    def _rpc(self, name, args):
        return self._call("POST", f"rpc/{name}", body=args)

    # --- salts, rate limits, nonces
    def salt(self, kind, period):
        return bytes.fromhex(_unbytea(self._rpc("get_or_create_salt", {"p_kind": kind, "p_period": period})))

    def rate_hit(self, key_hex, window_start, limit):
        return bool(self._rpc("rate_hit", {"p_key": _bytea(key_hex), "p_window": window_start, "p_limit": limit}))

    def use_nonce(self, nonce_hex, expires):
        import datetime

        iso = datetime.datetime.fromtimestamp(expires, datetime.timezone.utc).isoformat()
        return bool(self._rpc("use_nonce", {"p_hash": _bytea(nonce_hex), "p_expires": iso}))

    # --- phones and monitors
    def upsert_phone(self, key):
        filters = {f: eq(key[f]) for f in key}
        rows = self._select("phones", filters, limit=1)
        if not rows:
            self._call("POST", "phones", body=key, prefer="resolution=ignore-duplicates,return=minimal",
                       query={"on_conflict": ",".join(key)})
            rows = self._select("phones", filters, limit=1)
        return rows[0]

    def get_phone(self, pid):
        rows = self._select("phones", {"id": eq(pid)}, limit=1)
        return rows[0] if rows else None

    def update_phone(self, pid, fields):
        self._call("PATCH", "phones", {"id": eq(pid)}, fields, prefer="return=minimal")

    def list_phones(self, status=None):
        return self._select("phones", {"status": eq(status)} if status else None)

    @staticmethod
    def _monitor_out(row):
        return dict(row, edid_manufacturer=row["edid_manufacturer"] or None,
                    edid_product_code=None if row["edid_product_code"] < 0 else row["edid_product_code"],
                    model_name=row["model_name"] or None)

    def upsert_monitor(self, mon):
        ident = {"edid_manufacturer": mon.get("manufacturer") or "", "edid_product_code": mon.get("product_code", -1),
                 "model_name": mon.get("model_name") or ""}
        if ident == {"edid_manufacturer": "", "edid_product_code": -1, "model_name": ""}:
            return None
        filters = {k: eq(v) for k, v in ident.items()}
        rows = self._select("monitors", filters, limit=1)
        if not rows:
            self._call("POST", "monitors", body=dict(ident, screen_tech=mon.get("screen_tech", "unknown")),
                       prefer="resolution=ignore-duplicates,return=minimal", query={"on_conflict": ",".join(ident)})
            rows = self._select("monitors", filters, limit=1)
        return self._monitor_out(rows[0])

    def update_monitor(self, mid, fields):
        self._call("PATCH", "monitors", {"id": eq(mid)}, fields, prefer="return=minimal")

    def list_monitors(self, status=None):
        return [self._monitor_out(r) for r in self._select("monitors", {"status": eq(status)} if status else None)]

    def search_monitors(self, q, limit):
        pattern = "*" + q.replace("*", "").replace(",", "").replace("(", "").replace(")", "") + "*"
        rows = self._select("monitors", {"status": eq("verified"), "model_name": f"ilike.{pattern}"}, limit=limit)
        return [self._monitor_out(r) for r in rows]

    # --- submissions
    def insert_submission(self, row):
        row = dict(row, contributor_hash=_bytea(row["contributor_hash"]), deletion_hash=_bytea(row["deletion_hash"]))
        out = self._call("POST", "submissions", body=row, prefer="return=representation", query={"select": "id"})
        return out[0]["id"]

    def list_submissions(self, status=None, phone_id=None, monitor_id=None):
        filters = {}
        if status:
            filters["status"] = eq(status)
        if phone_id is not None:
            filters["phone_id"] = eq(phone_id)
        if monitor_id is not None:
            filters["or"] = f"(reference_monitor_id.eq.{int(monitor_id)},target_monitor_id.eq.{int(monitor_id)})"
        rows = self._select("submissions", filters, columns=SUBMISSION_COLUMNS, order="id.asc")
        for r in rows:
            r["contributor_hash"] = _unbytea(r["contributor_hash"])
            r["deletion_hash"] = _unbytea(r["deletion_hash"])
        return rows

    def update_submission(self, sid, fields):
        self._call("PATCH", "submissions", {"id": eq(sid)}, fields, prefer="return=minimal")

    def delete_by_deletion_hash(self, hash_hex):
        rows = self._call("DELETE", "submissions", {"deletion_hash": eq(_bytea(hash_hex)), "select": "phone_id"},
                          prefer="return=representation") or []
        for table in ("settings_samples", "reference_samples"):
            self._call("DELETE", table, {"deletion_hash": eq(_bytea(hash_hex))}, prefer="return=minimal")
        return [r["phone_id"] for r in rows]

    def _keys(self, table):
        rows = self._select(table, columns="edid_manufacturer,edid_product_code,picture_mode")
        return sorted({(r["edid_manufacturer"], r["edid_product_code"], r["picture_mode"]) for r in rows})

    def settings_keys(self):
        return self._keys("settings_samples")

    def reference_keys(self):
        return self._keys("reference_samples")

    def insert_reference_sample(self, row):
        row = dict(row, contributor_hash=_bytea(row["contributor_hash"]), deletion_hash=_bytea(row["deletion_hash"]))
        out = self._call("POST", "reference_samples", body=row, prefer="return=representation", query={"select": "id"})
        return out[0]["id"]

    def list_reference_samples(self, manufacturer, product_code, picture_mode):
        rows = self._select("reference_samples", {"edid_manufacturer": eq(manufacturer), "edid_product_code": eq(product_code),
                                                  "picture_mode": eq(picture_mode)}, order="id.asc")
        for r in rows:
            r["contributor_hash"] = _unbytea(r["contributor_hash"])
        return rows

    # --- monitor settings people chose to share
    def insert_settings_sample(self, row):
        row = dict(row, contributor_hash=_bytea(row["contributor_hash"]), deletion_hash=_bytea(row["deletion_hash"]))
        out = self._call("POST", "settings_samples", body=row, prefer="return=representation", query={"select": "id"})
        return out[0]["id"]

    def list_settings_samples(self, manufacturer, product_code, picture_mode):
        rows = self._select("settings_samples", {"edid_manufacturer": eq(manufacturer), "edid_product_code": eq(product_code),
                                                 "picture_mode": eq(picture_mode)}, order="id.asc")
        for r in rows:
            r["contributor_hash"] = _unbytea(r["contributor_hash"])
        return rows

    # --- corrections
    @staticmethod
    def _key_filters(phone_id, monitor_id, tech):
        return {"phone_id": eq(phone_id), "monitor_id": "is.null" if monitor_id is None else eq(monitor_id),
                "screen_tech": eq(tech or "")}

    def get_correction(self, phone_id, monitor_id, tech):
        rows = self._select("corrections", self._key_filters(phone_id, monitor_id, tech), limit=1)
        return rows[0] if rows else None

    def put_correction(self, row):
        filters = self._key_filters(row["phone_id"], row.get("monitor_id"), row.get("screen_tech"))
        body = {k: v for k, v in row.items() if k != "id"}
        if self._select("corrections", filters, limit=1):
            self._call("PATCH", "corrections", filters, body, prefer="return=minimal")
        else:
            self._call("POST", "corrections", body=body, prefer="return=minimal")

    def delete_correction(self, phone_id, monitor_id, tech):
        self._call("DELETE", "corrections", self._key_filters(phone_id, monitor_id, tech), prefer="return=minimal")

    def list_corrections(self, frozen=None, phone_id=None):
        filters = {}
        if frozen is not None:
            filters["frozen"] = eq("true" if frozen else "false")
        if phone_id is not None:
            filters["phone_id"] = eq(phone_id)
        return self._select("corrections", filters)
