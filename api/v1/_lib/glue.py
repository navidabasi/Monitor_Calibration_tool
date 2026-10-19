"""Connects Vercel's Python runtime to service.handle().

Vercel finds a function by the literal `class handler(BaseHTTPRequestHandler)` in each file under api/, so the
entry files define that class themselves and hand every request to serve() here.
"""

import functools
import json
import sys

from . import service
from .config import MAX_BODY, Context, config_from_env

sys.setrecursionlimit(1000)  # deeply nested JSON must fail fast, not exhaust the stack


@functools.lru_cache(maxsize=1)
def get_context():
    """Built once per warm function instance from environment variables (secrets never live in code)."""
    import os

    from .supabase_store import SupabaseStore

    return Context(config=config_from_env(), store=SupabaseStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]))


def serve(request, route, method, context=None):
    """Answer one request of a BaseHTTPRequestHandler for the endpoint `route`."""
    length = int(request.headers.get("content-length") or 0)
    if length > MAX_BODY:
        status, payload, headers = service._answer(413)
    else:
        body = request.rfile.read(length) if length > 0 else b""
        _, _, query = request.path.partition("?")
        try:
            ctx = (context or get_context)()
            status, payload, headers = service.handle(
                ctx, route, method, query, {k.lower(): v for k, v in request.headers.items()}, body)
        except Exception:  # missing configuration or database trouble: a plain error, never details
            import traceback

            traceback.print_exc()
            status, payload, headers = service._answer(500)
    out = b"" if status == 204 else json.dumps(payload).encode()
    request.send_response(status)
    for k, v in headers.items():
        request.send_header(k, v)
    request.send_header("Content-Length", str(len(out)))
    request.end_headers()
    request.wfile.write(out)
