"""GET /api/v1/coverage (see docs/ARCHITECTURE.md)."""

import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the project root: api/ and monitor_calibration/

from api.v1._lib.glue import serve  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        serve(self, "coverage", "GET")

    def do_POST(self):
        serve(self, "coverage", "POST")

    def do_OPTIONS(self):
        serve(self, "coverage", "OPTIONS")

    def log_message(self, *args):  # request lines can carry addresses; nothing is logged
        pass
