"""Connects Vercel's Python runtime (a BaseHTTPRequestHandler class) to service.handle()."""

import functools
import sys
from http.server import BaseHTTPRequestHandler

from . import service
from .config import MAX_BODY, Context, config_from_env

sys.setrecursionlimit(1000)  # deeply nested JSON must fail fast, not exhaust the stack


@functools.lru_cache(maxsize=1)
def get_context():
    """Built once per warm function instance from environment variables (secrets never live in code)."""
    import os

    from .supabase_store import SupabaseStore

    return Context(config=config_from_env(), store=SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]))


def make_handler(route, context=get_context):
    class handler(BaseHTTPRequestHandler):
        server_version = "mc-api"
        sys_version = ""

        def _run(self, method):
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_BODY:
                body = b""
                status, payload, headers = service._answer(413)
            else:
                body = self.rfile.read(length) if length > 0 else b""
                path, _, query = self.path.partition("?")
                status, payload, headers = service.handle(
                    context(), route, method, query, {k.lower(): v for k, v in self.headers.items()}, body)
            out = b"" if status == 204 else __import__("json").dumps(payload).encode()
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

        def log_message(self, *args):  # request lines can carry addresses; nothing is logged
            pass

    return handler
