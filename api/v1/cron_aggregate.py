"""POST/GET /api/v1/cron/aggregate (see docs/ARCHITECTURE.md)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the project root: api/ and monitor_calibration/

from api.v1._lib.glue import make_handler  # noqa: E402

handler = make_handler("cron_aggregate")
