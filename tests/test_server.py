"""The local server must only answer its own page.

    .venv\\Scripts\\python -m unittest discover -s tests -v
"""

import http.client
import json
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor_calibration import server  # noqa: E402


class LocalServerSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = server.make_server(0, cls.tmp.name)  # port 0: any free port
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        page = cls.request("GET", "/")[2]
        cls.token = re.search(r'name="mc-token" content="([^"]+)"', page).group(1)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    @classmethod
    def request(cls, method, path, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{cls.port}")
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.putheader("Content-Length", "0")
        conn.endheaders()
        res = conn.getresponse()
        body = res.read().decode("utf-8", "replace")
        conn.close()
        return res.status, dict(res.getheaders()), body

    def api(self, method, path, **headers):
        h = {"X-MC-Token": self.token, "Sec-Fetch-Site": "same-origin", "Origin": f"http://127.0.0.1:{self.port}"}
        h.update(headers)
        return self.request(method, path, {k: v for k, v in h.items() if v is not None})

    def test_page_gets_a_fresh_token(self):
        self.assertNotEqual(self.token, "__MC_TOKEN__")
        self.assertGreaterEqual(len(self.token), 40)
        other = server.make_server(0, self.tmp.name)
        self.assertNotEqual(other.token, self.srv.token)
        other.server_close()

    def test_own_page_works(self):
        status, _, body = self.api("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertIn("photos", json.loads(body))
        self.assertEqual(self.api("POST", "/api/reset")[0], 200)

    def test_missing_or_wrong_token_is_rejected(self):
        for token in (None, "", "guess", self.token[:-1] + "x"):
            for method, path in (("GET", "/api/session"), ("POST", "/api/reset"), ("POST", "/api/photo/1/delete")):
                status, _, body = self.api(method, path, **{"X-MC-Token": token})
                self.assertEqual(status, 403, (token, path))
                self.assertIn("Reload", json.loads(body)["error"])

    def test_other_websites_are_rejected(self):
        # A page on another site POSTing to the local server (CSRF), even if it somehow had the token.
        self.assertEqual(self.api("POST", "/api/reset", Origin="https://evil.example")[0], 403)
        self.assertEqual(self.api("POST", "/api/reset", **{"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.api("POST", "/api/reset", **{"Sec-Fetch-Site": "same-site"})[0], 403)

    def test_dns_rebinding_host_is_rejected(self):
        # evil.example resolving to 127.0.0.1: the browser sends Host: evil.example.
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1", f"127.0.0.1:{self.port + 1}"):
            self.assertEqual(self.request("GET", "/", host=host)[0], 403, host)
            self.assertEqual(self.request("GET", "/api/session", {"X-MC-Token": self.token}, host=host)[0], 403, host)
        self.assertEqual(self.request("GET", "/", host=f"localhost:{self.port}")[0], 200)

    def test_page_cannot_be_framed(self):
        _, headers, _ = self.request("GET", "/")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", headers.get("Content-Security-Policy", ""))
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_static_files_still_served(self):
        for path in ("/app.js", "/style.css"):
            self.assertEqual(self.request("GET", path)[0], 200, path)
        self.assertEqual(self.request("GET", "/../server.py")[0], 404)
        self.assertEqual(self.request("GET", "/../../pyproject.toml")[0], 404)


if __name__ == "__main__":
    unittest.main()
