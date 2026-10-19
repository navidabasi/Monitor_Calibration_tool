"""The database as the API sees it, and an in-memory copy of it for tests and local development.

SupabaseStore (supabase_store.py) talks to the real database with the
service-role key. MemoryStore behaves the same way, including what the
database's scheduled jobs delete, so the logic above it can be tested without
a database.

Hashes travel as lowercase hex strings. Dates are ISO strings.
"""

import copy
import os

PHONE_FIELDS = ("unique_camera_model", "lens_model", "raw_type", "app_family")
NO_PHONE_ID = 0


class MemoryStore:
    def __init__(self):
        self._salts = {}
        self._rate = {}
        self._nonces = {}
        self.phones = {}
        self.monitors = {}
        self.submissions = {}
        self.corrections = {}
        self.settings_samples = {}
        self.reference_samples = {}
        self._ids = {"phone": 0, "monitor": 0, "submission": 0, "sample": 0, "refsample": 0}

    # --- salts, rate limits, nonces
    def salt(self, kind, period):
        return self._salts.setdefault((kind, period), os.urandom(32))

    def rate_hit(self, key_hex, window_start, limit):
        k = (key_hex, window_start)
        self._rate[k] = self._rate.get(k, 0) + 1
        return self._rate[k] <= limit

    def use_nonce(self, nonce_hex, expires):
        if nonce_hex in self._nonces:
            return False
        self._nonces[nonce_hex] = expires
        return True

    # --- phones and monitors
    def _next(self, kind):
        self._ids[kind] += 1
        return self._ids[kind]

    def upsert_phone(self, key):
        for row in self.phones.values():
            if all(row[f] == key[f] for f in PHONE_FIELDS):
                return copy.deepcopy(row)
        pid = self._next("phone")
        self.phones[pid] = dict(key, id=pid, status="pending", embedded_color_matrix=None)
        return copy.deepcopy(self.phones[pid])

    def get_phone(self, pid):
        return copy.deepcopy(self.phones.get(pid))

    def update_phone(self, pid, fields):
        self.phones[pid].update(fields)

    def list_phones(self, status=None):
        return [copy.deepcopy(p) for p in self.phones.values() if status in (None, p["status"])]

    def upsert_monitor(self, mon):
        ident = (mon.get("manufacturer"), mon.get("product_code"), mon.get("model_name"))
        if ident == (None, None, None):
            return None
        for row in self.monitors.values():
            if (row["edid_manufacturer"], row["edid_product_code"], row["model_name"]) == ident:
                return copy.deepcopy(row)
        mid = self._next("monitor")
        self.monitors[mid] = {"id": mid, "edid_manufacturer": ident[0], "edid_product_code": ident[1], "model_name": ident[2],
                              "screen_tech": mon.get("screen_tech", "unknown"), "status": "pending"}
        return copy.deepcopy(self.monitors[mid])

    def update_monitor(self, mid, fields):
        self.monitors[mid].update(fields)

    def list_monitors(self, status=None):
        return [copy.deepcopy(m) for m in self.monitors.values() if status in (None, m["status"])]

    def search_monitors(self, q, limit):
        q = q.lower()
        hits = [m for m in self.monitors.values() if m["status"] == "verified" and q in (m["model_name"] or "").lower()]
        return [copy.deepcopy(m) for m in hits[:limit]]

    # --- submissions
    def insert_submission(self, row):
        sid = self._next("submission")
        self.submissions[sid] = dict(copy.deepcopy(row), id=sid)
        return sid

    def list_submissions(self, status=None, phone_id=None, monitor_id=None):
        return [copy.deepcopy(s) for s in self.submissions.values()
                if status in (None, s["status"]) and phone_id in (None, s["phone_id"])
                and monitor_id in (None, s["reference_monitor_id"], s["target_monitor_id"])]

    def update_submission(self, sid, fields):
        self.submissions[sid].update(fields)

    def delete_by_deletion_hash(self, hash_hex):
        ids = [i for i, s in self.submissions.items() if s["deletion_hash"] == hash_hex]
        phones = [self.submissions[i]["phone_id"] for i in ids]
        for i in ids:
            del self.submissions[i]
        for table in (self.settings_samples, self.reference_samples):
            for i in [i for i, s in table.items() if s["deletion_hash"] == hash_hex]:
                del table[i]
        return phones

    # --- monitor settings people chose to share
    def insert_settings_sample(self, row):
        sid = self._next("sample")
        self.settings_samples[sid] = dict(copy.deepcopy(row), id=sid)
        return sid

    def settings_keys(self):
        return sorted({(s["edid_manufacturer"], s["edid_product_code"], s["picture_mode"]) for s in self.settings_samples.values()})

    # --- measured reference profiles (ICC or report values) people used for a monitor model and picture mode
    def insert_reference_sample(self, row):
        sid = self._next("refsample")
        self.reference_samples[sid] = dict(copy.deepcopy(row), id=sid)
        return sid

    def list_reference_samples(self, manufacturer, product_code, picture_mode):
        return [copy.deepcopy(s) for s in self.reference_samples.values()
                if (s["edid_manufacturer"], s["edid_product_code"], s["picture_mode"]) == (manufacturer, product_code, picture_mode)]

    def reference_keys(self):
        return sorted({(s["edid_manufacturer"], s["edid_product_code"], s["picture_mode"]) for s in self.reference_samples.values()})

    def list_settings_samples(self, manufacturer, product_code, picture_mode):
        return [copy.deepcopy(s) for s in self.settings_samples.values()
                if (s["edid_manufacturer"], s["edid_product_code"], s["picture_mode"]) == (manufacturer, product_code, picture_mode)]

    # --- corrections
    @staticmethod
    def _ckey(phone_id, monitor_id, tech):
        return (phone_id, monitor_id or 0, tech or "")

    def get_correction(self, phone_id, monitor_id, tech):
        return copy.deepcopy(self.corrections.get(self._ckey(phone_id, monitor_id, tech)))

    def put_correction(self, row):
        self.corrections[self._ckey(row["phone_id"], row.get("monitor_id"), row.get("screen_tech"))] = copy.deepcopy(row)

    def delete_correction(self, phone_id, monitor_id, tech):
        self.corrections.pop(self._ckey(phone_id, monitor_id, tech), None)

    def list_corrections(self, frozen=None, phone_id=None):
        return [copy.deepcopy(c) for c in self.corrections.values()
                if frozen in (None, c["frozen"]) and phone_id in (None, c["phone_id"])]

    # --- what the database's scheduled jobs do
    def purge(self, now_ts, today, month):
        """Mirror supabase/migrations: expire nonces and rate rows, null old contributor hashes, drop old salts."""
        self._nonces = {n: e for n, e in self._nonces.items() if e >= now_ts}
        cutoff48 = today_minus(today, 2)
        self._rate = {k: v for k, v in self._rate.items() if k[1][:10] >= cutoff48}
        cutoff60 = today_minus(today, 60)
        for s in list(self.submissions.values()) + list(self.settings_samples.values()) + list(self.reference_samples.values()):
            if s["received_on"] < cutoff60:
                s["contributor_hash"] = None
        for (kind, period) in list(self._salts):
            if (kind == "day" and period < cutoff48) or (kind == "month" and period < today_minus(today, 90)[:7]):
                del self._salts[(kind, period)]
        cutoff30 = today_minus(today, 30)
        for sid in [i for i, s in self.submissions.items() if s["status"] == "pending" and s["received_on"] < cutoff30]:
            del self.submissions[sid]


def today_minus(today, days):
    import datetime

    return (datetime.date.fromisoformat(today) - datetime.timedelta(days=days)).isoformat()
