"""The `monitor-calibration` command: start the local site and open it in the browser."""

import argparse
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .server import make_server

DEFAULT_PORT = 8765

BROWSERS = {
    "win32": [
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
}
BROWSER_COMMANDS = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"]


def user_data_home():
    """Where an installed copy keeps sessions and its browser profile (never inside the install folder)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "MonitorCalibration"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MonitorCalibration"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "monitor-calibration"


def find_browser():
    for p in BROWSERS.get(sys.platform, []):
        path = Path(os.path.expanduser(os.path.expandvars(p)))
        if path.is_file():
            return str(path)
    for name in BROWSER_COMMANDS:
        found = shutil.which(name)
        if found:
            return found
    return None


def open_browser(url, profile):
    """Open the site in Chrome/Edge with color management off, so charts reach the monitor unchanged."""
    exe = find_browser()
    if not exe:
        print("Chrome or Edge wasn't found. Open this address in a Chromium browser started with")
        print("--force-color-profile=srgb:", url)
        webbrowser.open(url)
        return
    # A separate profile, so the flag applies even if the browser is already open.
    subprocess.Popen([
        exe,
        "--force-color-profile=srgb",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--app={url}?srgb=1",
    ])


def start_server(port, data_dir, fixed):
    """Start on `port`; if it's taken and the user didn't ask for it, try the next few."""
    tries = 1 if fixed else 20
    for p in range(port, port + tries):
        try:
            return make_server(p, data_dir)
        except OSError:
            continue
    if fixed:
        raise SystemExit(f"Port {port} is already in use. Close the other copy or pick another port with --port.")
    raise SystemExit(f"Ports {port}-{port + tries - 1} are all in use. Pick a free one with --port.")


def main(argv=None, home=None):
    ap = argparse.ArgumentParser(
        prog="monitor-calibration",
        description="Calibrate your monitor with your phone camera. Starts a local site and opens it in your browser.",
    )
    ap.add_argument("--port", type=int, help=f"port to use (default: {DEFAULT_PORT}, or the next free one)")
    ap.add_argument("--no-browser", action="store_true", help="start the site without opening a browser window")
    ap.add_argument("--data", help="folder for sessions (default: your user data folder)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    home = Path(home) if home else user_data_home()
    data_dir = Path(args.data) if args.data else home / "sessions"
    port = DEFAULT_PORT if args.port is None else args.port
    server = start_server(port, data_dir, fixed=args.port is not None)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Monitor calibration {__version__} is running at {url}")
    print(f"Sessions are saved in {data_dir}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        open_browser(url, home / ".browser-profile")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
