"""Find the connected monitors by reading their EDID (the identity block each monitor reports).

  Windows  registry (Enum/DISPLAY), limited to the monitors WMI says are connected
  macOS    ioreg
  Linux    /sys/class/drm/*/edid

The EDID contains a serial number. parse() never returns it, so it can't be
shown, saved or sent by accident. Detection is best effort: anything that
fails just means no monitors are listed and the user picks by hand.
"""

import re
import subprocess
import sys
from pathlib import Path

HEADER = b"\x00\xff\xff\xff\xff\xff\xff\x00"


def parse(data):
    """Parse a 128-byte (or longer) EDID. Returns a dict, or None if it isn't valid.

    Keys: manufacturer (3 letters), product_code, model_name, year,
    chromaticity (CIE xy of the primaries and white), resolution ([w, h] of the
    preferred mode). The serial number (header bytes and descriptor 0xFF) is
    deliberately left out.
    """
    data = bytes(data)
    if len(data) < 128 or data[:8] != HEADER or sum(data[:128]) % 256 != 0:
        return None
    m = (data[8] << 8) | data[9]
    letters = [(m >> 10) & 31, (m >> 5) & 31, m & 31]
    if not all(1 <= v <= 26 for v in letters):
        return None
    out = {
        "manufacturer": "".join(chr(64 + v) for v in letters),
        "product_code": data[10] | (data[11] << 8),
    }
    year = 1990 + data[17]  # manufacture year (or model year, if the week byte is 0xFF): never the week
    if 1991 <= year <= 2100:
        out["year"] = year

    lows = [data[25] >> s & 3 for s in (6, 4, 2, 0)] + [data[26] >> s & 3 for s in (6, 4, 2, 0)]
    vals = [((data[27 + i] << 2) | lows[i]) / 1024 for i in range(8)]
    out["chromaticity"] = dict(zip(("rx", "ry", "gx", "gy", "bx", "by", "wx", "wy"), vals))

    for off in range(54, 126, 18):
        block = data[off:off + 18]
        if block[0] == 0 and block[1] == 0 and block[3] == 0xFC and "model_name" not in out:
            name = block[5:18].split(b"\n")[0].decode("ascii", "ignore").strip()
            if name:
                out["model_name"] = name
        elif (block[0] or block[1]) and "resolution" not in out:  # first detailed timing = preferred mode
            w = block[2] | ((block[4] & 0xF0) << 4)
            h = block[5] | ((block[7] & 0xF0) << 4)
            if w and h:
                out["resolution"] = [w, h]
    return out


def read_all():
    """EDIDs of the connected monitors, parsed. Empty if none can be read on this system."""
    try:
        if sys.platform == "win32":
            blobs = _windows()
        elif sys.platform == "darwin":
            blobs = _macos()
        else:
            blobs = _linux()
    except Exception:  # unsupported system, missing tool, permissions
        return []
    monitors, seen = [], set()
    for blob in blobs:
        parsed = parse(blob)
        if parsed is None:
            continue
        key = (parsed["manufacturer"], parsed["product_code"], parsed.get("model_name"), bytes(blob[12:16]))
        if key in seen:
            continue
        seen.add(key)
        monitors.append(parsed)
    return monitors


def _windows():
    """EDIDs from the registry, for the monitors Windows reports as connected right now.

    The registry also keeps every monitor ever plugged in, so WMI (WmiMonitorID)
    says which ones are active; its instance names match the registry keys.
    """
    import winreg

    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         r"Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID | ForEach-Object { $_.InstanceName }"],
        capture_output=True, text=True, timeout=20, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    active = {re.sub(r"_\d+$", "", line.strip()).upper() for line in out.splitlines() if line.strip()}
    blobs = []
    root = r"SYSTEM\CurrentControlSet\Enum\DISPLAY"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as display:
        for i in range(winreg.QueryInfoKey(display)[0]):
            model = winreg.EnumKey(display, i)
            with winreg.OpenKey(display, model) as model_key:
                for j in range(winreg.QueryInfoKey(model_key)[0]):
                    instance = winreg.EnumKey(model_key, j)
                    if "\\".join(("DISPLAY", model, instance)).upper() not in active:
                        continue
                    try:
                        with winreg.OpenKey(model_key, instance + r"\Device Parameters") as params:
                            blobs.append(winreg.QueryValueEx(params, "EDID")[0])
                    except OSError:
                        continue
    return blobs


def _macos():
    out = subprocess.run(["ioreg", "-lw0"], capture_output=True, text=True, timeout=10, check=False).stdout
    return [bytes.fromhex(h) for h in re.findall(r'"IODisplayEDID"\s*=\s*<([0-9a-fA-F]+)>', out)]


def _linux():
    blobs = []
    for path in sorted(Path("/sys/class/drm").glob("*/edid")):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if data:
            blobs.append(data)
    return blobs
