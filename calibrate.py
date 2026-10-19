"""Run from a source checkout without installing:

    python calibrate.py              start and open the browser
    python calibrate.py --no-browser start only

Same as the `monitor-calibration` command, except sessions and the browser
profile are kept next to this file (./sessions, ./.browser-profile).
"""

import sys
from pathlib import Path

from monitor_calibration.cli import main

if __name__ == "__main__":
    sys.exit(main(home=Path(__file__).resolve().parent))
