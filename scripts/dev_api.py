"""Run the hosted API on your own machine against an in-memory database, for development and tests.

    python scripts/dev_api.py [--port 8787]
    MONCAL_API_URL=http://127.0.0.1:8787/api/v1 monitor-calibration

Nothing is kept after it stops. It has no secrets, no Turnstile and a low
proof-of-work difficulty, so never expose it to a network. The real
deployment is Vercel + Supabase.
"""

import argparse
import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.v1._lib import service  # noqa: E402
from api.v1._lib.config import MAX_BODY, Config, Context  # noqa: E402
from api.v1._lib.store import MemoryStore  # noqa: E402

ROUTES = {
    "/api/v1/challenge": "challenge", "/api/v1/submissions": "submissions", "/api/v1/corrections": "corrections",
    "/api/v1/monitors/search": "monitors_search", "/api/v1/monitors/settings": "monitor_settings",
    "/api/v1/monitors/reference": "monitor_reference", "/api/v1/coverage": "coverage", "/api/v1/submissions/delete": "submissions_delete",
    "/api/v1/cron/aggregate": "cron_aggregate",
}


def make_dev_server(port=0, difficulty=10, cron_secret=None, clock=None):
    """(server, ctx): an HTTP server on 127.0.0.1 and the context behind it."""
    cfg = Config(pow_secret=secrets.token_bytes(32), ip_secret=secrets.token_bytes(32), pow_difficulty=difficulty,
                 cron_secret=cron_secret or secrets.token_hex(16))
    ctx = Context(config=cfg, store=MemoryStore())
    if clock:
        ctx.clock = clock
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _run(self, method):
            path, _, query = self.path.partition("?")
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(min(length, MAX_BODY + 1)) if length > 0 else b""
            route = ROUTES.get(path)
            with lock:
                status, payload, headers = service.handle(ctx, route or "nothing", method, query,
                                                          {k.lower(): v for k, v in self.headers.items()}, body)
            out = b"" if status == 204 else json.dumps(payload).encode()
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            self._run("GET")

        def do_POST(self):
            self._run("POST")

        def do_OPTIONS(self):
            self._run("OPTIONS")

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler), ctx


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    server, ctx = make_dev_server(args.port)
    print(f"Dev API on http://127.0.0.1:{server.server_address[1]}/api/v1  (in-memory; cron secret: {ctx.config.cron_secret})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
