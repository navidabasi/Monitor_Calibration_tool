"""The Vercel entry files under api/v1/: Vercel finds a function by the literal `class handler(BaseHTTPRequestHandler)`,
so each file must define it, and a real HTTP request through it must reach the endpoint logic."""

import http.client
import importlib
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.v1._lib import glue  # noqa: E402
from api.v1._lib.config import Config, Context  # noqa: E402
from api.v1._lib.store import MemoryStore  # noqa: E402

ENTRIES = sorted(p for p in (ROOT / "api" / "v1").glob("*.py") if not p.name.startswith("_"))


class Entries(unittest.TestCase):
    def test_every_entry_defines_the_handler_class_vercel_looks_for(self):
        self.assertGreaterEqual(len(ENTRIES), 9)
        for path in ENTRIES:
            self.assertIn("class handler(BaseHTTPRequestHandler)", path.read_text(encoding="utf-8"), path.name)
            module = importlib.import_module(f"api.v1.{path.stem}")
            self.assertTrue(issubclass(module.handler, BaseHTTPRequestHandler), path.name)

    def test_a_request_goes_through_an_entry_to_the_endpoint(self):
        ctx = Context(config=Config(pow_secret=b"p" * 32, ip_secret=b"i" * 32, pow_difficulty=8), store=MemoryStore())
        challenge = importlib.import_module("api.v1.challenge").handler
        # Serve the real entry class, with the in-memory database in place of Supabase.
        entry = type("Entry", (challenge,), {"do_POST": lambda self: glue.serve(self, "challenge", "POST", context=lambda: ctx)})
        server = ThreadingHTTPServer(("127.0.0.1", 0), entry)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", "/api/v1/challenge", body=b"{}", headers={"Content-Type": "application/json", "X-Real-IP": "203.0.113.9"})
            res = conn.getresponse()
            body = json.loads(res.read())
            self.assertEqual(res.status, 200)
            self.assertEqual(set(body), {"nonce", "difficulty", "expires", "sig"})
            self.assertEqual(res.getheader("Cache-Control"), "no-store")
        finally:
            server.shutdown()
            server.server_close()

    def test_missing_configuration_is_a_plain_500(self):
        import contextlib
        import io

        challenge = importlib.import_module("api.v1.challenge").handler
        server = ThreadingHTTPServer(("127.0.0.1", 0), challenge)  # no environment variables: get_context() fails
        threading.Thread(target=server.serve_forever, daemon=True).start()
        glue.get_context.cache_clear()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            with contextlib.redirect_stderr(io.StringIO()):
                conn.request("POST", "/api/v1/challenge", body=b"{}", headers={"Content-Type": "application/json"})
                res = conn.getresponse()
                body = json.loads(res.read())
            self.assertEqual((res.status, body), (500, {"error": "server_error"}))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
