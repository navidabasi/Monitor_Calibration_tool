"""The Supabase store builds the right requests. A fake transport stands in for the network, so this checks the
shape of every call (filters, bytea encoding, the service key) but not the database itself."""

import sys
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.v1._lib.supabase_store import PAGE, SupabaseStore  # noqa: E402


class Fake:
    def __init__(self, *answers):
        self.calls, self.answers = [], list(answers)

    def __call__(self, method, url, headers, body):
        parts = urllib.parse.urlsplit(url)
        self.calls.append({"method": method, "path": parts.path, "query": dict(urllib.parse.parse_qsl(parts.query)),
                           "headers": headers, "body": body})
        return self.answers.pop(0) if self.answers else None


def store(*answers):
    fake = Fake(*answers)
    return SupabaseStore("https://project.supabase.co/", "service-key", request=fake), fake


class SupabaseStoreTests(unittest.TestCase):
    def test_new_style_secret_keys_are_sent_only_as_apikey(self):
        for key, bearer in (("sb_secret_abc123", False), ("eyJhbGciOi.payload.sig", True)):
            s, fake = store([])
            s.__init__("https://project.supabase.co", key, request=fake)
            s.list_phones()
            headers = fake.calls[0]["headers"]
            self.assertEqual(headers["apikey"], key)
            self.assertEqual("Authorization" in headers, bearer)

    def test_every_call_carries_the_service_key_and_no_other_host(self):
        s, fake = store([])
        s.list_phones()
        call = fake.calls[0]
        self.assertEqual(call["headers"]["apikey"], "service-key")
        self.assertNotIn("Authorization", call["headers"])  # "service-key" isn't a JWT: see the test above
        self.assertEqual(call["path"], "/rest/v1/phones")

    def test_phone_is_looked_up_then_created(self):
        row = {"id": 7, "status": "pending"}
        s, fake = store([], None, [row])
        key = {"unique_camera_model": "iPhone", "lens_model": "", "raw_type": "bayer", "app_family": "apple"}
        self.assertEqual(s.upsert_phone(key), row)
        self.assertEqual([c["method"] for c in fake.calls], ["GET", "POST", "GET"])
        self.assertEqual(fake.calls[0]["query"]["unique_camera_model"], "eq.iPhone")
        self.assertEqual(fake.calls[0]["query"]["lens_model"], "eq.")
        self.assertIn("ignore-duplicates", fake.calls[1]["headers"]["Prefer"])

    def test_hashes_are_sent_as_bytea(self):
        s, fake = store([{"id": 41}])
        row = {"session_month": "2026-10-01", "contributor_hash": "ab" * 32, "deletion_hash": "cd" * 32, "phone_id": 1}
        self.assertEqual(s.insert_submission(row), 41)
        body = fake.calls[0]["body"]
        self.assertEqual(body["contributor_hash"], "\\x" + "ab" * 32)
        self.assertEqual(body["deletion_hash"], "\\x" + "cd" * 32)
        self.assertEqual(fake.calls[0]["headers"]["Prefer"], "return=representation")

    def test_submissions_come_back_without_the_big_columns_and_with_plain_hex(self):
        s, fake = store([{"id": 1, "contributor_hash": "\\xabcd", "deletion_hash": "\\x12"}])
        rows = s.list_submissions(status="pending", phone_id=3)
        self.assertEqual((rows[0]["contributor_hash"], rows[0]["deletion_hash"]), ("abcd", "12"))
        query = fake.calls[0]["query"]
        self.assertNotIn("measurements", query["select"])
        self.assertEqual((query["status"], query["phone_id"]), ("eq.pending", "eq.3"))

    def test_submissions_by_monitor(self):
        s, fake = store([])
        s.list_submissions(status="accepted", monitor_id="12")
        self.assertEqual(fake.calls[0]["query"]["or"], "(reference_monitor_id.eq.12,target_monitor_id.eq.12)")

    def test_settings_samples(self):
        s, fake = store([{"id": 3}], [{"id": 1, "contributor_hash": "\\xab"}], None)
        row = {"edid_manufacturer": "AUS", "contributor_hash": "ab" * 32, "deletion_hash": "cd" * 32}
        self.assertEqual(s.insert_settings_sample(row), 3)
        self.assertEqual(fake.calls[0]["path"], "/rest/v1/settings_samples")
        self.assertEqual(fake.calls[0]["body"]["deletion_hash"], "\\x" + "cd" * 32)
        rows = s.list_settings_samples("AUS", 4242, "srgb")
        self.assertEqual(rows[0]["contributor_hash"], "ab")
        q = fake.calls[1]["query"]
        self.assertEqual((q["edid_manufacturer"], q["edid_product_code"], q["picture_mode"]), ("eq.AUS", "eq.4242", "eq.srgb"))
        s.delete_by_deletion_hash("ef" * 32)  # also removes the settings samples
        self.assertEqual([c["path"] for c in fake.calls[2:]],
                         ["/rest/v1/submissions", "/rest/v1/settings_samples", "/rest/v1/reference_samples"])

    def test_reference_samples_and_keys(self):
        s, fake = store([{"id": 9}], [{"id": 1, "contributor_hash": "\\xab"}], [
            {"edid_manufacturer": "DEL", "edid_product_code": 1, "picture_mode": "srgb"},
            {"edid_manufacturer": "DEL", "edid_product_code": 1, "picture_mode": "srgb"},
            {"edid_manufacturer": "AUS", "edid_product_code": 2, "picture_mode": "game"}])
        self.assertEqual(s.insert_reference_sample({"contributor_hash": "ab" * 32, "deletion_hash": "cd" * 32}), 9)
        self.assertEqual(fake.calls[0]["path"], "/rest/v1/reference_samples")
        self.assertEqual(s.list_reference_samples("DEL", 1, "srgb")[0]["contributor_hash"], "ab")
        self.assertEqual(s.reference_keys(), [("AUS", 2, "game"), ("DEL", 1, "srgb")])  # each model and mode once
        self.assertEqual(fake.calls[2]["path"], "/rest/v1/reference_samples")

    def test_delete_by_code_returns_phone_ids(self):
        s, fake = store([{"phone_id": 3}, {"phone_id": 3}])
        self.assertEqual(s.delete_by_deletion_hash("ef" * 32), [3, 3])
        self.assertEqual(fake.calls[0]["method"], "DELETE")
        self.assertEqual(fake.calls[0]["query"]["deletion_hash"], "eq.\\x" + "ef" * 32)

    def test_corrections_keys(self):
        s, fake = store([], [{"id": 1, "frozen": False}])
        self.assertIsNone(s.get_correction(1, None, "woled"))
        self.assertEqual(fake.calls[0]["query"]["monitor_id"], "is.null")
        self.assertEqual(s.get_correction(1, 9, "woled")["id"], 1)
        self.assertEqual(fake.calls[1]["query"]["monitor_id"], "eq.9")

    def test_put_correction_updates_or_inserts(self):
        s, fake = store([{"id": 5}], None)
        s.put_correction({"phone_id": 1, "monitor_id": None, "screen_tech": "woled", "matrix": [1] * 9})
        self.assertEqual([c["method"] for c in fake.calls], ["GET", "PATCH"])
        s, fake = store([], None)
        s.put_correction({"id": 3, "phone_id": 1, "monitor_id": 2, "screen_tech": "woled", "matrix": [1] * 9})
        self.assertEqual([c["method"] for c in fake.calls], ["GET", "POST"])
        self.assertNotIn("id", fake.calls[1]["body"])

    def test_functions_are_called_through_rpc(self):
        s, fake = store("\\x" + "11" * 32, True, False)
        self.assertEqual(s.salt("day", "2026-10-19"), bytes.fromhex("11" * 32))
        self.assertTrue(s.rate_hit("aa" * 32, "2026-10-19T00:00:00Z", 10))
        self.assertFalse(s.use_nonce("bb" * 32, 1_792_000_000))
        self.assertEqual([c["path"] for c in fake.calls], ["/rest/v1/rpc/get_or_create_salt", "/rest/v1/rpc/rate_hit", "/rest/v1/rpc/use_nonce"])
        self.assertEqual(fake.calls[1]["body"]["p_key"], "\\x" + "aa" * 32)

    def test_monitor_search_cannot_inject_filters(self):
        s, fake = store([])
        s.search_monitors("a),status.eq.blocked,(b*", 10)
        pattern = fake.calls[0]["query"]["model_name"]
        self.assertEqual(pattern, "ilike.*a" + ",status.eq.blocked".replace(",", "") + "b*".replace("*", "") + "*")
        for ch in "(),":
            self.assertNotIn(ch, pattern)

    def test_monitor_defaults_map_to_none(self):
        s, _ = store([{"id": 1, "edid_manufacturer": "", "edid_product_code": -1, "model_name": "", "screen_tech": "unknown", "status": "pending"}])
        mon = s.list_monitors()[0]
        self.assertEqual((mon["edid_manufacturer"], mon["edid_product_code"], mon["model_name"]), (None, None, None))
        s, fake = store()
        self.assertIsNone(s.upsert_monitor({}))
        self.assertFalse(fake.calls)

    def test_long_lists_are_paged(self):
        first = [{"id": i, "status": "pending"} for i in range(PAGE)]
        s, fake = store(first, [{"id": PAGE, "status": "pending"}])
        self.assertEqual(len(s.list_phones()), PAGE + 1)
        self.assertEqual(fake.calls[1]["headers"]["Range"], f"{PAGE}-{2 * PAGE - 1}")


if __name__ == "__main__":
    unittest.main()
